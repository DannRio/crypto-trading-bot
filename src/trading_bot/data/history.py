from pathlib import Path
import re

import pandas as pd

from trading_bot.data.market_data import OHLCV_COLUMNS, normalize_ohlcv
from trading_bot.data.validation import latest_expected_close, validate_ohlcv
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
    return value * unit_ms[unit]


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


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

        # Pagination overlap is intentional; defects within an exchange page
        # are not. Validate before normalize_ohlcv can sort/deduplicate them.
        try:
            page = pd.DataFrame(candles, columns=OHLCV_COLUMNS)
            page["timestamp"] = pd.to_datetime(page["timestamp"], unit="ms", utc=True)
            quality = validate_ohlcv(page, timeframe=timeframe)
            single_unaligned = (
                len(page) == 1
                and latest_expected_close(page["timestamp"].iloc[0], timeframe)
                != page["timestamp"].iloc[0]
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid downloaded OHLCV page {batches}: {exc}") from exc
        if not quality.valid or quality.missing_intervals or single_unaligned:
            issues = [*quality.issues, *quality.warnings]
            if single_unaligned:
                issues.append("Candle timestamp is not aligned to the requested timeframe.")
            raise ValueError(f"Invalid downloaded OHLCV page {batches}: " + "; ".join(issues))

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

    result = normalize_ohlcv(rows)
    if not result.empty:
        quality = validate_ohlcv(result, timeframe=timeframe)
        if not quality.valid or quality.missing_intervals:
            raise ValueError("Invalid downloaded OHLCV range: " + "; ".join((*quality.issues, *quality.warnings)))
    return result


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
        # Read faithfully: research validation must see unsorted/duplicate rows.
        return df[OHLCV_COLUMNS].reset_index(drop=True)

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
                .sort_values("timestamp", kind="stable")
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

    (store or HistoricalDataStore()).upsert(
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        data=data,
    )
    return data
