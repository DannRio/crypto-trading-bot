import pandas as pd

from trading_bot.core.enums import Signal
from trading_bot.core.models import StrategyResult


def generate_signal(df: pd.DataFrame) -> StrategyResult:
    required_columns = [
        "close",
        "ema_50",
        "ema_200",
        "rsi_14",
        "atr_14",
    ]

    if len(df) < 2:
        return StrategyResult(
            signal=Signal.NO_TRADE,
            reason="Not enough candles.",
        )

    for column in required_columns:
        if column not in df.columns:
            return StrategyResult(
                signal=Signal.NO_TRADE,
                reason=f"Missing required column: {column}",
            )

    previous = df.iloc[-2]
    latest = df.iloc[-1]

    if latest[required_columns].isna().any():
        return StrategyResult(
            signal=Signal.NO_TRADE,
            reason="Latest candle contains incomplete indicator data.",
        )

    bullish_trend = (
        latest["close"] > latest["ema_50"]
        and latest["ema_50"] > latest["ema_200"]
    )

    bearish_trend = (
        latest["close"] < latest["ema_50"]
        and latest["ema_50"] < latest["ema_200"]
    )

    rsi_crossed_above_50 = (
        previous["rsi_14"] <= 50
        and latest["rsi_14"] > 50
    )

    rsi_crossed_below_50 = (
        previous["rsi_14"] >= 50
        and latest["rsi_14"] < 50
    )

    if bullish_trend and rsi_crossed_above_50:
        return StrategyResult(
            signal=Signal.LONG,
            reason="Bullish EMA structure and RSI crossed above 50.",
        )

    if bearish_trend and rsi_crossed_below_50:
        return StrategyResult(
            signal=Signal.SHORT,
            reason="Bearish EMA structure and RSI crossed below 50.",
        )

    return StrategyResult(
        signal=Signal.NO_TRADE,
        reason="Entry conditions are not aligned.",
    )