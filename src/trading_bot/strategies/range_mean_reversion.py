import math

import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.models import SetupAssessment


STRATEGY_NAME = "range_mean_reversion"


def _no_trade(reason: str) -> SetupAssessment:
    return SetupAssessment(
        signal=Signal.NO_TRADE,
        status=SetupStatus.NO_TRADE,
        score=0,
        reason=reason,
        reasons=(reason,),
        strategy=STRATEGY_NAME,
    )


def evaluate_range_mean_reversion(
    df: pd.DataFrame,
    structure: MarketStructure,
    min_confirmed_score: int = 70,
    min_forming_score: int = 50,
) -> SetupAssessment:
    required = {
        "open",
        "high",
        "low",
        "close",
        "rsi_14",
        "atr_14",
    }
    missing = required.difference(df.columns)
    if missing or df.empty:
        return _no_trade("Insufficient range mean-reversion data.")

    if structure.regime != MarketRegime.RANGE:
        return _no_trade(
            f"Regime is {structure.regime.value}; range strategy disabled."
        )

    latest = df.iloc[-1]
    if latest[list(required)].isna().any():
        return _no_trade("Latest range data is incomplete.")

    support = float(structure.support)
    resistance = float(structure.resistance)
    atr = float(latest["atr_14"])
    if (
        not math.isfinite(support)
        or not math.isfinite(resistance)
        or resistance <= support
        or atr <= 0
    ):
        return _no_trade("Range boundaries are not usable.")

    width = resistance - support
    if width < 2.0 * atr:
        return _no_trade("Range is too narrow relative to ATR.")

    close = float(latest["close"])
    open_price = float(latest["open"])
    low = float(latest["low"])
    high = float(latest["high"])
    rsi = float(latest["rsi_14"])
    position = (close - support) / width

    if position <= 0.30:
        signal = Signal.LONG
    elif position >= 0.70:
        signal = Signal.SHORT
    else:
        return _no_trade("Price is near the middle of the range.")

    score = 35
    reasons = [
        f"RANGE regime with price at {position * 100:.1f}% of the range."
    ]

    if signal == Signal.LONG:
        if position <= 0.20:
            score += 20
            reasons.append("Price is close to range support.")
        else:
            score += 10
            reasons.append("Price is in the lower part of the range.")

        if rsi <= 38:
            score += 25
            reasons.append("RSI is oversold for a range reversal.")
        elif rsi <= 45:
            score += 10
            reasons.append("RSI is soft near range support.")

        if low <= support + (0.25 * atr) and close > open_price:
            score += 20
            reasons.append("Latest candle rejected the support area.")
    else:
        if position >= 0.80:
            score += 20
            reasons.append("Price is close to range resistance.")
        else:
            score += 10
            reasons.append("Price is in the upper part of the range.")

        if rsi >= 62:
            score += 25
            reasons.append("RSI is overbought for a range reversal.")
        elif rsi >= 55:
            score += 10
            reasons.append("RSI is firm near range resistance.")

        if high >= resistance - (0.25 * atr) and close < open_price:
            score += 20
            reasons.append("Latest candle rejected the resistance area.")

    if width >= 4.0 * atr:
        score += 10
        reasons.append("Range has adequate width relative to ATR.")

    if structure.high_volatility:
        score -= 10
        reasons.append("Volatility is elevated; setup score reduced.")

    score = max(0, min(score, 100))
    if score >= min_confirmed_score:
        status = SetupStatus.CONFIRMED
    elif score >= min_forming_score:
        status = SetupStatus.FORMING
    else:
        status = SetupStatus.NO_TRADE
        signal = Signal.NO_TRADE

    return SetupAssessment(
        signal=signal,
        status=status,
        score=score,
        reason="; ".join(reasons),
        reasons=tuple(reasons),
        strategy=STRATEGY_NAME,
    )
