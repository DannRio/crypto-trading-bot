import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.models import SetupAssessment


STRATEGY_NAME = "breakout_retest"


def _no_trade(reason: str) -> SetupAssessment:
    return SetupAssessment(
        signal=Signal.NO_TRADE,
        status=SetupStatus.NO_TRADE,
        score=0,
        reason=reason,
        reasons=(reason,),
        strategy=STRATEGY_NAME,
    )


def evaluate_breakout_retest(
    df: pd.DataFrame,
    structure: MarketStructure,
    min_confirmed_score: int = 70,
    min_forming_score: int = 50,
    breakout_lookback: int = 20,
    retest_window: int = 4,
) -> SetupAssessment:
    required = {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "rsi_14",
        "atr_14",
    }
    missing = required.difference(df.columns)
    minimum = breakout_lookback + retest_window + 1
    if missing or len(df) < minimum:
        return _no_trade("Insufficient breakout/retest data.")

    latest = df.iloc[-1]
    if latest[list(required)].isna().any():
        return _no_trade("Latest breakout/retest data is incomplete.")

    prior = df.iloc[
        -(breakout_lookback + retest_window + 1):
        -(retest_window + 1)
    ]
    breakout_window = df.iloc[-(retest_window + 1):-1]

    upper_level = float(prior["high"].max())
    lower_level = float(prior["low"].min())
    atr = float(latest["atr_14"])
    close = float(latest["close"])
    low = float(latest["low"])
    high = float(latest["high"])
    rsi = float(latest["rsi_14"])

    if atr <= 0:
        return _no_trade("ATR is not usable for breakout/retest analysis.")

    long_breakout_rows = breakout_window[
        breakout_window["close"] > upper_level
    ]
    short_breakout_rows = breakout_window[
        breakout_window["close"] < lower_level
    ]

    has_long = not long_breakout_rows.empty
    has_short = not short_breakout_rows.empty
    if has_long == has_short:
        return _no_trade(
            "No unambiguous recent breakout requiring a retest."
        )

    if has_long:
        signal = Signal.LONG
        level = upper_level
        breakout_row = long_breakout_rows.iloc[-1]
        retest_touched = (
            (level - 0.75 * atr)
            <= low
            <= (level + 0.50 * atr)
        )
        retest_held = close >= level
    else:
        signal = Signal.SHORT
        level = lower_level
        breakout_row = short_breakout_rows.iloc[-1]
        retest_touched = (
            (level - 0.50 * atr)
            <= high
            <= (level + 0.75 * atr)
        )
        retest_held = close <= level

    score = 35
    reasons = [
        f"Recent {signal.value.lower()} breakout through {level:.6f}."
    ]

    if retest_touched and retest_held:
        score += 25
        reasons.append("Breakout level was retested and held.")
    elif retest_held:
        score += 10
        reasons.append("Price remains beyond the breakout level.")

    if signal == Signal.LONG:
        if 50 <= rsi <= 70:
            score += 15
            reasons.append("RSI supports bullish continuation.")
        elif rsi > 70:
            score += 5
            reasons.append("Bullish momentum is extended.")
        if structure.regime == MarketRegime.BULL_TREND:
            score += 10
            reasons.append("Market regime supports the long breakout.")
        if structure.higher_low:
            score += 10
            reasons.append("Recent structure shows a higher low.")
    else:
        if 30 <= rsi <= 50:
            score += 15
            reasons.append("RSI supports bearish continuation.")
        elif rsi < 30:
            score += 5
            reasons.append("Bearish momentum is extended.")
        if structure.regime == MarketRegime.BEAR_TREND:
            score += 10
            reasons.append("Market regime supports the short breakout.")
        if structure.lower_high:
            score += 10
            reasons.append("Recent structure shows a lower high.")

    volume_sma = breakout_row.get("volume_sma_20")
    if (
        pd.notna(volume_sma)
        and float(volume_sma) > 0
        and float(breakout_row["volume"]) / float(volume_sma) >= 1.10
    ):
        score += 15
        reasons.append("Breakout occurred on elevated volume.")

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
