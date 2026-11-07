from dataclasses import dataclass

import pandas as pd


REQUIRED_OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class DataQualityReport:
    valid: bool
    row_count: int
    duplicate_timestamps: int
    invalid_ohlc_rows: int
    nonpositive_price_rows: int
    negative_volume_rows: int
    missing_intervals: int
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    if len(timeframe) < 2:
        raise ValueError(f"Invalid timeframe: {timeframe}")
    value = int(timeframe[:-1])
    unit = timeframe[-1].lower()
    units = {"m": "min", "h": "h", "d": "D", "w": "W"}
    if unit not in units:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return pd.Timedelta(value, unit=units[unit])


def validate_ohlcv(
    df: pd.DataFrame,
    timeframe: str | None = None,
    min_rows: int = 1,
) -> DataQualityReport:
    issues: list[str] = []
    warnings: list[str] = []
    missing_columns = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing_columns:
        issues.append(f"Missing required columns: {', '.join(missing_columns)}")
        return DataQualityReport(
            False, len(df), 0, 0, 0, 0, 0, tuple(issues), tuple(warnings)
        )

    row_count = len(df)
    if row_count < min_rows:
        issues.append(f"Only {row_count} candles available; {min_rows} required.")

    duplicate_timestamps = int(df["timestamp"].duplicated().sum())
    invalid_ohlc = (
        (df["high"] < df["low"])
        | (df["high"] < df["open"])
        | (df["high"] < df["close"])
        | (df["low"] > df["open"])
        | (df["low"] > df["close"])
    )
    invalid_ohlc_rows = int(invalid_ohlc.sum())
    price_columns = ["open", "high", "low", "close"]
    nonpositive_price_rows = int((df[price_columns] <= 0).any(axis=1).sum())
    negative_volume_rows = int((df["volume"] < 0).sum())

    if duplicate_timestamps:
        issues.append(f"{duplicate_timestamps} duplicate timestamp(s) detected.")
    if invalid_ohlc_rows:
        issues.append(f"{invalid_ohlc_rows} invalid OHLC row(s) detected.")
    if nonpositive_price_rows:
        issues.append(f"{nonpositive_price_rows} non-positive price row(s) detected.")
    if negative_volume_rows:
        issues.append(f"{negative_volume_rows} negative-volume row(s) detected.")

    missing_intervals = 0
    if timeframe and row_count >= 2:
        expected = _timeframe_delta(timeframe)
        timestamps = pd.to_datetime(df["timestamp"], utc=True).sort_values().drop_duplicates()
        for diff in timestamps.diff().dropna():
            if diff > expected:
                missing_intervals += max(int(round(diff / expected)) - 1, 1)
        if missing_intervals:
            warnings.append(f"{missing_intervals} missing candle interval(s) detected.")

    return DataQualityReport(
        valid=not issues,
        row_count=row_count,
        duplicate_timestamps=duplicate_timestamps,
        invalid_ohlc_rows=invalid_ohlc_rows,
        nonpositive_price_rows=nonpositive_price_rows,
        negative_volume_rows=negative_volume_rows,
        missing_intervals=missing_intervals,
        issues=tuple(issues),
        warnings=tuple(warnings),
    )
