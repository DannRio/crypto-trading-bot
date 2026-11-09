from dataclasses import dataclass
from pathlib import Path
import re

import pandas as pd

from trading_bot.data.market_data import OHLCV_COLUMNS, normalize_ohlcv
from trading_bot.exchanges.ccxt_client import CCXTClient


def timeframe_to_milliseconds(timeframe: str) -> int:
    if len(timeframe) < 2:
        raise ValueError(f"Invalid timeframe: {timeframe}")

    value = int(timeframe[:-1])
    unit = timeframe[-1].lower()
    unit_ms = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 604_800_000,
    }
    if unit not in unit_ms:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if value <= 0:
        raise ValueError("Timeframe length must be positive.")
    return value * unit_ms[unit]


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")



@dataclass(frozen=True)
class HistoryCoverage:
    """Evidence of the coverage actually downloaded, not the requested dates."""
    requested_start: pd.Timestamp
    requested_end_exclusive: pd.Timestamp
    actual_start: pd.Timestamp | None
    actual_end: pd.Timestamp | None
    expected_candles: int
    actual_candles: int
    missing_candles: int
    complete: bool

    def summary(self) -> str:
        first = self.actual_start.isoformat() if self.actual_start is not None else "none"
        last = self.actual_end.isoformat() if self.actual_end is not None else "none"
        return (
            f"{self.actual_candles:,}/{self.expected_candles:,} candles; "
            f"actual {first} to {last}; {self.missing_candles:,} "
            "missing/out-of-range candles"
        )


def assess_history_coverage(
    data: pd.DataFrame, start, end, timeframe: str = "1h"
) -> HistoryCoverage:
    """Require each requested UTC candle-open timestamp to be present.

    An exchange may refuse old history or ignore the `since` argument.
    Missing leading/trailing periods are as important as gaps in the middle.
    The last requested timestamp is exclusive.
    """
    first = _utc_timestamp(start)
    last = _utc_timestamp(end)
    if last <= first:
        raise ValueError("end must be later than start.")
    delta = timeframe_to_milliseconds(timeframe)
    start_ms = int(first.timestamp() * 1000)
    end_ms = int(last.timestamp() * 1000)
    # CCXT weekly candles are anchored to Monday 00:00 UTC, rather than
    # the Thursday 1970-01-01 Unix epoch.
    origin_ms = 4 * 86_400_000 if timeframe.endswith("w") else 0
    first_expected_ms = (
        ((start_ms - origin_ms + delta - 1) // delta) * delta + origin_ms
    )
    expected = max(0, (end_ms - first_expected_ms + delta - 1) // delta)
    if expected <= 0:
        raise ValueError("Requested period contains no full candle timestamps.")

    if data.empty:
        actual = pd.DatetimeIndex([], tz="UTC")
    else:
        actual = pd.DatetimeIndex(pd.to_datetime(data["timestamp"], utc=True))
        actual = actual.sort_values().unique()
        actual = pd.DatetimeIndex(actual)
    in_range = actual[(actual >= first) & (actual < last)]
    aligned = all(
        (int(item.timestamp() * 1000) - origin_ms) % delta == 0
        for item in in_range
    )
    inside_count = len(in_range)
    missing = max(0, expected - inside_count)
    complete = (
        aligned and missing == 0 and len(actual) == inside_count
        and inside_count == expected
    )
    return HistoryCoverage(
        requested_start=first,
        requested_end_exclusive=last,
        actual_start=in_range.min() if inside_count else None,
        actual_end=in_range.max() if inside_count else None,
        expected_candles=expected,
        actual_candles=inside_count,
        missing_candles=missing,
        complete=complete,
    )


class IncompleteHistoryError(ValueError):
    """Exchange did not supply all requested candle timestamps."""


def fetch_ohlcv_range(
    symbol: str,
    start,
    end,
    timeframe: str = "1h",
    exchange_id: str = "kraken",
    market_type: str = "spot",
    batch_limit: int = 1000,
    client: CCXTClient | None = None,
    max_batches: int | None = None,
) -> pd.DataFrame:
    """Fetch an inclusive-start, exclusive-end OHLCV range with pagination."""
    if batch_limit <= 0:
        raise ValueError("batch_limit must be positive.")

    start_ts = _utc_timestamp(start)
    end_ts = _utc_timestamp(end)
    if end_ts <= start_ts:
        raise ValueError("end must be later than start.")

    timeframe_ms = timeframe_to_milliseconds(timeframe)
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)

    exchange = client or CCXTClient(
        exchange_id=exchange_id,
        market_type=market_type,
    )

    rows: list[list] = []
    since = start_ms
    batches = 0

    while since < end_ms:
        candles = exchange.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            limit=batch_limit,
            since=since,
        )
        batches += 1

        if not candles:
            break

        rows.extend(
            candle
            for candle in candles
            if start_ms <= int(candle[0]) < end_ms
        )

        last_timestamp = max(int(candle[0]) for candle in candles)
        next_since = last_timestamp + timeframe_ms
        if next_since <= since:
            break

        since = next_since
        if last_timestamp >= end_ms - timeframe_ms:
            break
        if max_batches is not None and batches >= max_batches:
            break

    return normalize_ohlcv(rows)


def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", symbol)


class HistoricalDataStore:
    """CSV-backed local research store with deterministic dataset paths."""

    def __init__(self, root: str | Path = "data/history"):
        self.root = Path(root)

    def dataset_path(
        self,
        exchange_id: str,
        market_type: str,
        symbol: str,
        timeframe: str,
    ) -> Path:
        return (
            self.root
            / exchange_id.lower()
            / market_type.lower()
            / _safe_symbol(symbol)
            / f"{timeframe}.csv"
        )

    def load(
        self,
        exchange_id: str,
        market_type: str,
        symbol: str,
        timeframe: str,
    ) -> pd.DataFrame:
        path = self.dataset_path(
            exchange_id,
            market_type,
            symbol,
            timeframe,
        )
        if not path.exists():
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        df = pd.read_csv(path)
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return (
            df[OHLCV_COLUMNS]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

    def upsert(
        self,
        exchange_id: str,
        market_type: str,
        symbol: str,
        timeframe: str,
        data: pd.DataFrame,
    ) -> Path:
        path = self.dataset_path(
            exchange_id,
            market_type,
            symbol,
            timeframe,
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = self.load(
            exchange_id,
            market_type,
            symbol,
            timeframe,
        )

        combined = pd.concat([existing, data], ignore_index=True)
        if combined.empty:
            combined = pd.DataFrame(columns=OHLCV_COLUMNS)
        else:
            combined["timestamp"] = pd.to_datetime(
                combined["timestamp"],
                utc=True,
            )
            combined = (
                combined[OHLCV_COLUMNS]
                .sort_values("timestamp")
                .drop_duplicates(subset=["timestamp"], keep="last")
                .reset_index(drop=True)
            )

        combined.to_csv(path, index=False)
        return path


def download_history(
    symbol: str,
    start,
    end,
    timeframe: str = "1h",
    exchange_id: str = "kraken",
    market_type: str = "spot",
    batch_limit: int = 1000,
    store: HistoricalDataStore | None = None,
    client: CCXTClient | None = None,
    strict_coverage: bool = True,
) -> pd.DataFrame:
    data = fetch_ohlcv_range(
        symbol=symbol,
        start=start,
        end=end,
        timeframe=timeframe,
        exchange_id=exchange_id,
        market_type=market_type,
        batch_limit=batch_limit,
        client=client,
    )

    report = assess_history_coverage(data, start, end, timeframe)
    if strict_coverage and not report.complete:
        raise IncompleteHistoryError(
            f"{exchange_id} {market_type} {symbol} {timeframe}: "
            f"incomplete history requested from {start} to {end}. "
            f"{report.summary()}. Exchange history may be limited; "
            "use --allow-partial only when incomplete coverage is acceptable."
        )

    (store or HistoricalDataStore()).upsert(
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        data=data,
    )
    return data


def import_history_csv(
    file_path: str | Path,
    start,
    end,
    symbol: str,
    timeframe: str,
    exchange_id: str,
    market_type: str,
    store: HistoricalDataStore | None = None,
    strict_coverage: bool = True,
) -> HistoryCoverage:
    """Import a locally obtained, six-column CSV after strict validation.

    Required headers: timestamp, open, high, low, close, volume. Timestamp
    accepts ISO-8601 UTC, Unix seconds or Unix milliseconds. The CSV's source
    and market classification must be independently verified by the user.
    """
    from trading_bot.data.validation import validate_ohlcv

    path = Path(file_path)
    frame = pd.read_csv(path)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    missing = set(OHLCV_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"CSV missing OHLCV columns: {sorted(missing)}")
    frame = frame[OHLCV_COLUMNS].copy()
    if frame.empty:
        raise ValueError("CSV contains no candles.")

    raw_time = frame["timestamp"]
    numeric = pd.to_numeric(raw_time, errors="coerce")
    if numeric.notna().all():
        # Milliseconds and seconds are the standard CCXT / exchange formats.
        unit = "ms" if numeric.abs().median() >= 100_000_000_000 else "s"
        frame["timestamp"] = pd.to_datetime(
            numeric, unit=unit, utc=True, errors="coerce"
        )
    else:
        frame["timestamp"] = pd.to_datetime(
            raw_time, utc=True, errors="coerce"
        )
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.isna().any().any():
        raise ValueError("CSV contains missing or invalid timestamps/numbers.")
    if not frame["timestamp"].is_unique:
        raise ValueError("CSV contains duplicate timestamps.")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    quality = validate_ohlcv(frame, timeframe=timeframe, min_rows=1)
    if not quality.valid:
        raise ValueError("CSV has invalid OHLCV data: " + "; ".join(quality.issues))

    report = assess_history_coverage(frame, start, end, timeframe)
    if strict_coverage and not report.complete:
        raise IncompleteHistoryError(
            f"Imported CSV does not cover requested history: {report.summary()}."
        )
    first, last = _utc_timestamp(start), _utc_timestamp(end)
    frame = frame.loc[
        (frame["timestamp"] >= first) & (frame["timestamp"] < last)
    ].copy()
    (store or HistoricalDataStore()).upsert(
        exchange_id, market_type, symbol, timeframe, frame
    )
    return report
