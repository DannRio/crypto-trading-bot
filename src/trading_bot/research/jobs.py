"""Bounded, explicit preparation of the operator policy's historical inputs."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from threading import Event, RLock, Thread
import time
from uuid import uuid4

import pandas as pd

from trading_bot.config import INDICATOR_WINDOW
from trading_bot.data.history import HistoricalDataStore, fetch_ohlcv_range, timeframe_to_milliseconds
from trading_bot.data.validation import latest_expected_close, validate_ohlcv
from trading_bot.exchanges import get_public_client


log = logging.getLogger(__name__)
EXCHANGES = {"kucoin", "kraken", "binance", "bybit", "okx", "coinbase"}
PAIR = re.compile(r"^[A-Z0-9._-]+/(USD|USDT|USDC)(?::(USD|USDT|USDC))?$")


@dataclass(frozen=True)
class HistoryRequest:
    exchange: str
    market_type: str
    symbol: str
    start: str
    end: str

    def __post_init__(self):
        if self.exchange not in EXCHANGES or self.market_type not in {"spot", "swap"}:
            raise ValueError("Unsupported historical market.")
        if not isinstance(self.symbol, str) or len(self.symbol) > 50 or not PAIR.fullmatch(self.symbol):
            raise ValueError("Use an exact trading pair for history preparation.")
        if self.market_type == "spot" and ":" in self.symbol:
            raise ValueError("Choose a spot pair for a spot dataset.")
        for value in (self.start, self.end):
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Use dates in YYYY-MM-DD format (UTC).")
        try:
            start, end = pd.Timestamp(self.start, tz="UTC"), pd.Timestamp(self.end, tz="UTC") + pd.Timedelta(days=1)
        except (ValueError, OverflowError) as exc:
            raise ValueError("Invalid historical dates.") from exc
        if not pd.Timedelta(0) < end - start <= pd.Timedelta(days=731):
            raise ValueError("Choose a historical period of at most two years.")


class HistoryJobConflict(ValueError):
    pass


class HistoryJobs:
    def __init__(self, root: str | Path, *, client_factory=None, fetcher=None, now=None):
        self.store = HistoricalDataStore(root)
        self.client_factory = client_factory or get_public_client
        self.fetcher = fetcher or fetch_ohlcv_range
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._jobs = {}
        self._worker = None
        self._cancel = Event()

    def start(self, request: HistoryRequest):
        current = pd.Timestamp(self.now())
        if current.tzinfo is None:
            raise ValueError("History preparation requires a UTC-aware clock.")
        requested_start = pd.Timestamp(request.start, tz="UTC")
        requested_end = pd.Timestamp(request.end, tz="UTC") + pd.Timedelta(days=1)
        effective_end = min(requested_end, latest_expected_close(current, "4h"))
        if effective_end <= requested_start:
            raise ValueError("Choose an earlier start; there are no fully closed 4h intervals in this period yet.")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise HistoryJobConflict("A history preparation job is already running.")
            job_id = str(uuid4())
            self._cancel = Event()
            job = {"job_id": job_id, "state": "running", "message": "Preparing matching 4h, 1h and 15m history.",
                "progress": [], "requested_start": request.start, "requested_end": request.end,
                "effective_end": effective_end.isoformat(), "end_clamped": effective_end < requested_end,
                "warmup_candles": INDICATOR_WINDOW,
                "warmup_start": (requested_start - pd.Timedelta(hours=4 * INDICATOR_WINDOW)).isoformat(),
                "exchange": request.exchange, "market_type": request.market_type, "symbol": request.symbol,
                "error": None, "started_at": current.isoformat()}
            self._jobs[job_id] = job
            while len(self._jobs) > 20:
                self._jobs.pop(next(iter(self._jobs)))
            self._worker = Thread(target=self._prepare, args=(job_id, request, requested_start, effective_end, self._cancel),
                                  daemon=True, name="history-preparation")
            self._worker.start()
            return deepcopy(job)

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def _update(self, job_id, **values):
        with self._lock:
            self._jobs[job_id].update(values)

    def _prepare(self, job_id, request, start, end, cancel):
        progress = []
        deadline = time.monotonic() + 1200
        try:
            client = self.client_factory(request.exchange, request.market_type)
            owner = self

            class BoundedClient:
                def fetch_ohlcv(self, **kwargs):
                    if cancel.is_set():
                        raise InterruptedError("History preparation was stopped.")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("History preparation exceeded its time budget.")
                    owner._update(job_id, message=f"Downloading {kwargs.get('timeframe')} candles; earlier saved data is preserved.")
                    return client.fetch_ohlcv(**kwargs)

            for timeframe in ("4h", "1h", "15m"):
                if cancel.is_set():
                    raise InterruptedError("History preparation was stopped.")
                delta = pd.to_timedelta(timeframe_to_milliseconds(timeframe), unit="ms")
                warmup_start = latest_expected_close(start, timeframe) - INDICATOR_WINDOW * delta
                frame = self.fetcher(symbol=request.symbol, start=warmup_start, end=end,
                    timeframe=timeframe, exchange_id=request.exchange, market_type=request.market_type,
                    batch_limit=500, max_batches=700, client=BoundedClient())
                if not isinstance(frame, pd.DataFrame) or frame.empty:
                    raise ValueError(f"The exchange returned no {timeframe} history for this period.")
                quality = validate_ohlcv(frame, timeframe=timeframe, min_rows=INDICATOR_WINDOW + 1)
                times = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
                if not quality.valid or quality.missing_intervals or times.isna().any():
                    raise ValueError(f"The {timeframe} history is invalid or has missing intervals.")
                if times.iloc[0] != warmup_start or times.iloc[-1] + delta != end:
                    raise ValueError(f"The exchange did not supply the complete {timeframe} period and indicator warmup.")
                if cancel.is_set():
                    raise InterruptedError("History preparation was stopped.")
                self.store.upsert(request.exchange, request.market_type, request.symbol, timeframe, frame)
                progress.append({"timeframe": timeframe, "rows": len(frame),
                    "start": times.iloc[0].isoformat(), "end": (times.iloc[-1] + delta).isoformat()})
                self._update(job_id, progress=deepcopy(progress))
            self._update(job_id, state="completed", message=f"All three validated datasets are ready through {end.isoformat()}. Run validation explicitly.",
                         finished_at=pd.Timestamp(self.now()).isoformat())
        except Exception as exc:
            log.error("History preparation failed (%s).", type(exc).__name__)
            if isinstance(exc, (ValueError, InterruptedError, TimeoutError)) and str(exc).startswith((
                "The exchange", "The 4h", "The 1h", "The 15m", "History preparation")):
                message = str(exc)
            else:
                message = "History preparation failed. Check market availability, requested dates, and data-directory permissions."
            self._update(job_id, state="error", message=message, error=message,
                         finished_at=pd.Timestamp(self.now()).isoformat())

    def shutdown(self, timeout=2):
        with self._lock:
            self._cancel.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
        return worker is None or not worker.is_alive()
