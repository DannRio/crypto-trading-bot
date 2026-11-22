from dataclasses import dataclass

import numpy as np

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
    if value <= 0:
        raise ValueError("Timeframe must be positive.")
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

    # Coerce only for validation. Never silently repair input used for execution.
    numeric = df[list(REQUIRED_OHLCV_COLUMNS[1:])].apply(pd.to_numeric, errors="coerce")
    bad_numeric = int((~np.isfinite(numeric.to_numpy(dtype=float))).any(axis=1).sum())
    if bad_numeric:
        issues.append(f"{bad_numeric} non-finite or non-numeric OHLCV row(s) detected.")
    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        issues.append("Invalid or missing candle timestamp(s) detected.")
    valid_times = timestamps.dropna()
    if not valid_times.is_monotonic_increasing:
        issues.append("Candle timestamps must be sorted in increasing order.")
    duplicate_timestamps = int(timestamps.duplicated().sum())
    df = df.copy()
    df[list(REQUIRED_OHLCV_COLUMNS[1:])] = numeric
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
        # Weekly candles are anchored to Monday, other fixed intervals to epoch UTC.
        origin = pd.Timestamp("1970-01-05", tz="UTC") if timeframe.endswith("w") else pd.Timestamp("1970-01-01", tz="UTC")
        if not valid_times.empty and ((valid_times - origin) % expected != pd.Timedelta(0)).any():
            issues.append("Candle timestamp(s) are not aligned to the requested timeframe.")
        timestamps = valid_times.sort_values().drop_duplicates()
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


def latest_expected_close(as_of, timeframe: str) -> pd.Timestamp:
    """Latest scheduled close at/before as_of for UTC fixed-interval candles."""
    stamp = pd.Timestamp(as_of)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    delta = _timeframe_delta(timeframe)
    origin = pd.Timestamp("1970-01-05", tz="UTC") if timeframe.endswith("w") else pd.Timestamp("1970-01-01", tz="UTC")
    return origin + ((stamp - origin) // delta) * delta
