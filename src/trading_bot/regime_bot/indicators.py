from __future__ import annotations

import numpy as np
import pandas as pd

from .config import IndicatorConfig
from .validation import prepare_ohlcv


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    average_gain = gains.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + relative_strength))
    both_zero = (average_gain == 0.0) & (average_loss == 0.0)
    result = result.where(average_loss != 0.0, 100.0)
    result = result.where(average_gain != 0.0, 0.0)
    return result.where(~both_zero, 50.0)


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(frame).ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()


def adx(frame: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    upward = frame["high"].diff()
    downward = -frame["low"].diff()
    plus_dm = upward.where((upward > downward) & (upward > 0.0), 0.0)
    minus_dm = downward.where((downward > upward) & (downward > 0.0), 0.0)

    smoothed_tr = true_range(frame).ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    smoothed_plus = plus_dm.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    smoothed_minus = minus_dm.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    plus_di = 100.0 * smoothed_plus / smoothed_tr.replace(0.0, np.nan)
    minus_di = 100.0 * smoothed_minus / smoothed_tr.replace(0.0, np.nan)
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator.replace(0.0, np.nan)
    dx = dx.where(denominator != 0.0, 0.0)
    adx_series = dx.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    return adx_series, plus_di, minus_di


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_line = ema(series, fast)
    slow_line = ema(series, slow)
    macd_line = fast_line - slow_line
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    def rank_last(values: np.ndarray) -> float:
        if len(values) == 0 or not np.isfinite(values[-1]):
            return np.nan
        finite = values[np.isfinite(values)]
        if len(finite) == 0:
            return np.nan
        return float(np.count_nonzero(finite <= values[-1]) / len(finite))

    return series.rolling(window=window, min_periods=max(20, window // 3)).apply(
        rank_last,
        raw=True,
    )


def build_features(data: pd.DataFrame, config: IndicatorConfig) -> pd.DataFrame:
    frame = prepare_ohlcv(data)
    close = frame["close"]

    frame["ema_fast"] = ema(close, config.ema_fast)
    frame["ema_mid"] = ema(close, config.ema_mid)
    frame["ema_slow"] = ema(close, config.ema_slow)
    frame["ema_fast_slope"] = (
        frame["ema_fast"] / frame["ema_fast"].shift(config.slope_lookback) - 1.0
    )
    frame["ema_mid_slope"] = (
        frame["ema_mid"] / frame["ema_mid"].shift(config.slope_lookback) - 1.0
    )
    frame["ema_slow_slope"] = (
        frame["ema_slow"] / frame["ema_slow"].shift(config.slope_lookback) - 1.0
    )

    frame["rsi"] = rsi(close, config.rsi_period)
    frame["atr"] = atr(frame, config.atr_period)
    frame["atr_percent"] = frame["atr"] / close
    frame["volatility_percentile"] = _rolling_percentile_rank(
        frame["atr_percent"],
        config.volatility_window,
    )

    frame["adx"], frame["plus_di"], frame["minus_di"] = adx(
        frame,
        config.adx_period,
    )
    frame["macd"], frame["macd_signal"], frame["macd_histogram"] = macd(
        close,
        config.macd_fast,
        config.macd_slow,
        config.macd_signal,
    )

    frame["volume_average"] = (
        frame["volume"]
        .rolling(
            config.volume_period,
            min_periods=config.volume_period,
        )
        .mean()
        .shift(1)
    )
    frame["volume_ratio"] = frame["volume"] / frame["volume_average"].replace(0.0, np.nan)

    frame["donchian_high"] = frame["high"].rolling(
        config.donchian_period,
        min_periods=config.donchian_period,
    ).max().shift(1)
    frame["donchian_low"] = frame["low"].rolling(
        config.donchian_period,
        min_periods=config.donchian_period,
    ).min().shift(1)
    frame["donchian_mid"] = (frame["donchian_high"] + frame["donchian_low"]) / 2.0

    frame["swing_high"] = frame["high"].rolling(
        config.swing_period,
        min_periods=config.swing_period,
    ).max().shift(1)
    frame["swing_low"] = frame["low"].rolling(
        config.swing_period,
        min_periods=config.swing_period,
    ).min().shift(1)

    frame["body_atr"] = (frame["close"] - frame["open"]).abs() / frame["atr"].replace(
        0.0,
        np.nan,
    )
    frame["distance_fast_ema_atr"] = (
        frame["close"] - frame["ema_fast"]
    ) / frame["atr"].replace(0.0, np.nan)
    frame["return_1"] = close.pct_change()
    return frame
