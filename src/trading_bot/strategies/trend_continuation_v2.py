"""Trend continuation v2: only events, never an ongoing EMA/RSI state.

The legacy trend_continuation strategy is intentionally kept as a baseline.
Scores rank qualifying events; they cannot substitute for an event.
"""
import pandas as pd

from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.models import SetupAssessment
from trading_bot.strategies.events import attach_event


def evaluate_trend_continuation_v2(
    df: pd.DataFrame, structure,
    min_confirmed_score: int = 70,
    min_forming_score: int = 50,
    breakout_atr: float = 0.15,
    pullback_atr: float = 0.75,
    min_volume_ratio: float = 1.10,
    max_extension_atr: float = 3.0,
    impulse_lookback: int = 8,
) -> SetupAssessment:
    if min(breakout_atr, pullback_atr, min_volume_ratio, max_extension_atr) <= 0 or impulse_lookback < 3:
        raise ValueError("Trend event parameters must be positive; impulse_lookback >= 3.")
    name = "trend_continuation_v2"
    required = {"open", "high", "low", "close", "ema_50", "ema_200", "rsi_14", "atr_14"}
    if len(df) < 3 or not required.issubset(df.columns):
        return SetupAssessment(Signal.NO_TRADE, SetupStatus.NO_TRADE, 0,
                               "Insufficient trend v2 data.", ("Insufficient trend v2 data.",), name)
    prev, latest = df.iloc[-2], df.iloc[-1]
    if pd.isna(latest[list(required)]).any() or pd.isna(prev[list(required)]).any():
        return SetupAssessment(Signal.NO_TRADE, SetupStatus.NO_TRADE, 0,
                               "Incomplete trend indicators.", ("Incomplete trend indicators.",), name)
    atr = float(latest["atr_14"])
    if atr <= 0:
        return SetupAssessment(Signal.NO_TRADE, SetupStatus.NO_TRADE, 0,
                               "ATR must be positive.", ("ATR must be positive.",), name)
    if structure.regime == MarketRegime.BULL_TREND:
        signal, direction = Signal.LONG, 1
    elif structure.regime == MarketRegime.BEAR_TREND:
        signal, direction = Signal.SHORT, -1
    else:
        return SetupAssessment(Signal.NO_TRADE, SetupStatus.NO_TRADE, 0,
                               "No directional trend.", ("No directional trend.",), name)
    close = float(latest["close"])
    previous_close = float(prev["close"])
    rsi = float(latest["rsi_14"])
    previous_rsi = float(prev["rsi_14"])
    ema = float(latest["ema_50"])
    previous_ema = float(prev["ema_50"])
    # Require a FRESH breach of the previous 20-bar level, not simply price
    # being above a level for many candles.
    level = float(structure.resistance if direction == 1 else structure.support)
    breakout = (
        direction * (close - level) > breakout_atr * atr
        and direction * (previous_close - level) <= 0
    )
    breakout_confirmed = breakout and structure.volume_ratio >= min_volume_ratio
    # A pullback must touch the EMA on the prior completed candle and resume
    # through the prior candle's extreme, with directional momentum.
    pullback_touch = (
        abs(float(prev["low"] if direction == 1 else prev["high"]) - previous_ema)
        <= pullback_atr * atr
    )
    resumed = (
        close > float(prev["high"]) and close > float(latest["open"])
        if direction == 1 else
        close < float(prev["low"]) and close < float(latest["open"])
    )
    momentum = (
        50 < rsi <= 68 and rsi >= previous_rsi
        if direction == 1 else
        32 <= rsi < 50 and rsi <= previous_rsi
    )
    impulse = df.iloc[-(impulse_lookback + 2):-2]
    # A prior directional impulse and a retracement are distinct from an EMA touch.
    impulse_ok = len(impulse) >= 3 and (
        float(impulse["high"].max()) - float(prev["low"]) >= atr
        and float(impulse["close"].iloc[-1]) > float(impulse["ema_50"].iloc[-1])
        if direction == 1 else
        float(prev["high"]) - float(impulse["low"].min()) >= atr
        and float(impulse["close"].iloc[-1]) < float(impulse["ema_50"].iloc[-1])
    )
    extension_ok = 0 < direction * (close - ema) <= max_extension_atr * atr
    breakout_confirmed = breakout_confirmed and momentum and extension_ok
    pullback_confirmed = pullback_touch and resumed and momentum and impulse_ok and extension_ok
    if not (breakout_confirmed or pullback_confirmed):
        reason = "Waiting for new volume-confirmed breakout or EMA pullback resumption."
        return SetupAssessment(
            signal, SetupStatus.FORMING, 45, reason, (reason,), name
        )
    score = 55
    reasons = ["Fresh breakout." if breakout_confirmed else "EMA pullback and resumption."]
    if momentum:
        score += 15
        reasons.append("RSI supports directional resumption.")
    if structure.volume_ratio >= min_volume_ratio:
        score += 15
        reasons.append("Volume expanded.")
    if (direction == 1 and structure.higher_low) or (direction == -1 and structure.lower_high):
        score += 10
        reasons.append("Swing structure aligns.")
    if structure.high_volatility:
        score -= 15
        reasons.append("High volatility penalty.")
    score = max(0, min(100, score))
    status = (SetupStatus.CONFIRMED if score >= min_confirmed_score
              else SetupStatus.FORMING if score >= min_forming_score
              else SetupStatus.NO_TRADE)
    assessment = SetupAssessment(
        signal if status != SetupStatus.NO_TRADE else Signal.NO_TRADE,
        status, score, "; ".join(reasons), tuple(reasons), name
    )
    if status == SetupStatus.NO_TRADE:
        return assessment
    subtype = "trend_breakout" if breakout_confirmed else "trend_pullback"
    anchor = level if breakout_confirmed else previous_ema
    structural_extreme = (
        min(float(prev["low"]), float(latest["low"])) if direction == 1 else
        max(float(prev["high"]), float(latest["high"]))
    )
    invalidation = structural_extreme - direction * 0.25 * atr
    return attach_event(assessment, df, setup_type=subtype, anchor=anchor,
                        invalidation=invalidation,
                        cancel=level if breakout_confirmed else invalidation)
