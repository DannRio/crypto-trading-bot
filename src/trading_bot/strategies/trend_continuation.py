import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.models import SetupAssessment


def evaluate_trend_continuation(
    df: pd.DataFrame,
    structure: MarketStructure,
    min_confirmed_score: int = 70,
    min_forming_score: int = 50,
    long_rsi_max: float = 68.0,
    short_rsi_min: float = 32.0,
    pullback_atr: float = 0.75,
    volume_ratio_threshold: float = 1.10,
    high_volatility_penalty: int = 10,
) -> SetupAssessment:
    required = {"close", "ema_50", "ema_200", "rsi_14", "atr_14"}
    missing = required.difference(df.columns)
    if missing or len(df) < 2:
        return SetupAssessment(
            signal=Signal.NO_TRADE,
            status=SetupStatus.NO_TRADE,
            score=0,
            reason="Insufficient strategy data.",
            reasons=("Insufficient strategy data.",),
        )

    previous = df.iloc[-2]
    latest = df.iloc[-1]
    if latest[list(required)].isna().any():
        return SetupAssessment(
            signal=Signal.NO_TRADE,
            status=SetupStatus.NO_TRADE,
            score=0,
            reason="Latest indicator data is incomplete.",
            reasons=("Latest indicator data is incomplete.",),
        )

    if structure.regime == MarketRegime.BULL_TREND:
        signal = Signal.LONG
    elif structure.regime == MarketRegime.BEAR_TREND:
        signal = Signal.SHORT
    else:
        reason = f"Regime is {structure.regime.value}; no trend setup."
        return SetupAssessment(
            signal=Signal.NO_TRADE,
            status=SetupStatus.NO_TRADE,
            score=0,
            reason=reason,
            reasons=(reason,),
        )

    score = 35
    reasons: list[str] = [
        f"{structure.regime.value.replace('_', ' ').title()} alignment."
    ]

    rsi = float(latest["rsi_14"])
    previous_rsi = float(previous["rsi_14"])
    close = float(latest["close"])
    ema_50 = float(latest["ema_50"])
    atr = float(latest["atr_14"])

    if signal == Signal.LONG:
        if 50 <= rsi <= long_rsi_max:
            score += 20
            reasons.append("RSI confirms bullish momentum without extension.")
        elif rsi > long_rsi_max:
            score += 5
            reasons.append("Bullish momentum is extended.")

        if previous_rsi <= 50 < rsi:
            score += 10
            reasons.append("RSI crossed above 50.")

        if close > structure.resistance:
            score += 20
            reasons.append("Price closed above prior resistance.")
        elif close >= ema_50 and (close - ema_50) <= (pullback_atr * atr):
            score += 15
            reasons.append("Price is holding a controlled pullback near EMA 50.")

        if structure.higher_low:
            score += 10
            reasons.append("Recent structure printed a higher low.")

    else:
        if short_rsi_min <= rsi <= 50:
            score += 20
            reasons.append("RSI confirms bearish momentum without extension.")
        elif rsi < short_rsi_min:
            score += 5
            reasons.append("Bearish momentum is extended.")

        if previous_rsi >= 50 > rsi:
            score += 10
            reasons.append("RSI crossed below 50.")

        if close < structure.support:
            score += 20
            reasons.append("Price closed below prior support.")
        elif close <= ema_50 and (ema_50 - close) <= (pullback_atr * atr):
            score += 15
            reasons.append("Price is holding a controlled rally near EMA 50.")

        if structure.lower_high:
            score += 10
            reasons.append("Recent structure printed a lower high.")

    if structure.volume_ratio >= volume_ratio_threshold:
        score += 15
        reasons.append(f"Volume is {structure.volume_ratio:.2f}x its recent average.")

    if structure.high_volatility:
        score -= high_volatility_penalty
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
    )
