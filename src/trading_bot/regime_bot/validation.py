from __future__ import annotations

import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
_TIMEFRAME_PATTERN = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[mhdw])$")


class DataValidationError(ValueError):
    """Raised when OHLCV data is unsafe to analyze."""


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    match = _TIMEFRAME_PATTERN.fullmatch(timeframe.strip().lower())
    if not match:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}. Use values such as 15m, 1h, 4h, 1d, or 1w."
        )
    count = int(match.group("count"))
    unit = match.group("unit")
    mapping = {
        "m": "minutes",
        "h": "hours",
        "d": "days",
        "w": "weeks",
    }
    return pd.Timedelta(**{mapping[unit]: count})


def prepare_ohlcv(data: pd.DataFrame, *, minimum_bars: int | None = None) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame):
        raise DataValidationError("OHLCV input must be a pandas DataFrame.")
    missing = [column for column in REQUIRED_COLUMNS if column not in data.columns]
    if missing:
        raise DataValidationError(f"Missing required OHLCV columns: {missing}")

    frame = data.loc[:, REQUIRED_COLUMNS].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame.isna().any().any():
        bad_columns = frame.columns[frame.isna().any()].tolist()
        raise DataValidationError(f"OHLCV data contains missing or invalid values in {bad_columns}.")

    frame = (
        frame.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )

    numeric = frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise DataValidationError("OHLCV data contains non-finite numeric values.")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataValidationError("OHLC prices must be strictly positive.")
    if (frame["volume"] < 0).any():
        raise DataValidationError("Volume cannot be negative.")

    invalid_high = frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    invalid_low = frame["low"] > frame[["open", "close", "high"]].min(axis=1)
    if invalid_high.any() or invalid_low.any():
        raise DataValidationError("OHLC price relationships are invalid for one or more candles.")

    if minimum_bars is not None and len(frame) < minimum_bars:
        raise DataValidationError(
            f"Need at least {minimum_bars} closed candles; received {len(frame)}."
        )
    return frame


def drop_incomplete_candle(
    data: pd.DataFrame,
    timeframe: str,
    *,
    now: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if data.empty:
        return data.copy()
    frame = prepare_ohlcv(data)
    current_time = pd.Timestamp(now or datetime.now(timezone.utc))
    if current_time.tzinfo is None:
        current_time = current_time.tz_localize("UTC")
    else:
        current_time = current_time.tz_convert("UTC")

    duration = timeframe_to_timedelta(timeframe)
    latest_open = frame.iloc[-1]["timestamp"]
    if latest_open + duration > current_time:
        return frame.iloc[:-1].reset_index(drop=True)
    return frame


def ensure_unique_timeframes(frames: dict[str, pd.DataFrame]) -> None:
    if not frames:
        raise DataValidationError("At least one timeframe is required.")
    normalized: set[str] = set()
    for timeframe in frames:
        value = timeframe.strip().lower()
        timeframe_to_timedelta(value)
        if value in normalized:
            raise DataValidationError(f"Duplicate timeframe: {value}")
        normalized.add(value)
