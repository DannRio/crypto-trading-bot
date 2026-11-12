import pandas as pd
from trading_bot.config import INDICATOR_WINDOW


def add_ema(df: pd.DataFrame, periods: tuple[int, ...] = (50, 200)) -> pd.DataFrame:
    result = df.copy()
    for period in periods:
        result[f"ema_{period}"] = result["close"].ewm(span=period, adjust=False).mean()
    return result


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    result = df.copy()
    delta = result["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    result[f"rsi_{period}"] = 100 - (100 / (1 + rs))
    return result


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    result = df.copy()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result[f"atr_{period}"] = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    return result


def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    result = df.copy()
    result[f"volume_sma_{period}"] = (
        result["volume"].rolling(window=period, min_periods=period).mean()
    )
    return result


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    result = add_ema(df)
    result = add_rsi(result)
    result = add_atr(result)
    result = add_volume_sma(result)
    return result


def decision_indicators(df: pd.DataFrame, window: int = INDICATOR_WINDOW) -> pd.DataFrame:
    """Use the same causal raw-candle window in live and historical decisions."""
    if type(window) is not int or window < 21:
        raise ValueError("Indicator window must contain at least 21 candles.")
    raw = df.tail(window).copy()
    return add_indicators(raw)
