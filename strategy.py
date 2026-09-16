from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class StrategyResult:
    signal: Signal
    reason: str


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
            Signal.NO_TRADE,
            "Not enough candles."
        )

    for column in required_columns:
        if column not in df.columns:
            return StrategyResult(
                Signal.NO_TRADE,
                f"Missing required column: {column}"
            )

    previous = df.iloc[-2]
    latest = df.iloc[-1]

    if latest[required_columns].isna().any():
        return StrategyResult(
            Signal.NO_TRADE,
            "Latest candle contains incomplete indicator data."
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
            Signal.LONG,
            "Bullish EMA structure and RSI crossed above 50."
        )

    if bearish_trend and rsi_crossed_below_50:
        return StrategyResult(
            Signal.SHORT,
            "Bearish EMA structure and RSI crossed below 50."
        )

    return StrategyResult(
        Signal.NO_TRADE,
        "Entry conditions are not aligned."
    )