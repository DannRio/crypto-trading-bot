import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.models import SetupAssessment
from trading_bot.strategies.events import attach_event


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

    if breakout_lookback < 2 or retest_window < 1:
        raise ValueError("Invalid breakout/retest lookbacks.")
    latest = df.iloc[-1]
    atr = float(latest["atr_14"])
    close, low, high = (float(latest[k]) for k in ("close", "low", "high"))
    rsi = float(latest["rsi_14"])
    if atr <= 0:
        return _no_trade("ATR is not usable for breakout/retest analysis.")

    events = []
    for index in range(len(df) - retest_window - 1, len(df) - 1):
        prior = df.iloc[index - breakout_lookback:index]
        row = df.iloc[index]
        upper, lower = float(prior["high"].max()), float(prior["low"].min())
        breakout_atr = float(row.get("atr_14", atr))
        if float(row["close"]) > upper + 0.15 * breakout_atr:
            events.append((index, Signal.LONG, upper, row))
        elif float(row["close"]) < lower - 0.15 * breakout_atr:
            events.append((index, Signal.SHORT, lower, row))
    if not events or len({item[1] for item in events}) != 1:
        return _no_trade("No unambiguous fresh breakout requiring a retest.")
    # The first breach owns the thesis; later bars cannot move its anchor.
    breakout_index, signal, level, breakout_row = events[0]
    since_breakout = df.iloc[breakout_index + 1:]
    if signal == Signal.LONG:
        held_path = bool((since_breakout["close"] >= level).all())
        retest_touched = level - 0.75 * atr <= low <= level + 0.50 * atr
        retest_held = close >= level and close > float(latest["open"])
        regime_ok = structure.regime != MarketRegime.BEAR_TREND
        invalidation = min(level, low) - 0.25 * atr
    else:
        held_path = bool((since_breakout["close"] <= level).all())
        retest_touched = level - 0.50 * atr <= high <= level + 0.75 * atr
        retest_held = close <= level and close < float(latest["open"])
        regime_ok = structure.regime != MarketRegime.BULL_TREND
        invalidation = max(level, high) + 0.25 * atr
    if not (held_path and retest_touched and retest_held and regime_ok):
        return _no_trade("Retest must touch, reject and hold the original breakout level; scores cannot override failure.")

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

    assessment = SetupAssessment(
        signal=signal,
        status=status,
        score=score,
        reason="; ".join(reasons),
        reasons=tuple(reasons),
        strategy=STRATEGY_NAME,
    )
    if status == SetupStatus.NO_TRADE:
        return assessment
    return attach_event(assessment, df, setup_type="breakout_retest", anchor=level,
                        invalidation=invalidation, event_index=breakout_index,
                        cancel=level)
