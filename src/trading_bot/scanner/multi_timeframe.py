from dataclasses import dataclass, replace

import pandas as pd

from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.data import DataQualityReport, timeframe_to_milliseconds
from trading_bot.strategies import SetupAssessment


@dataclass(frozen=True)
class TimeframeConfirmation:
    timeframe: str
    regime: MarketRegime | None
    aligned: bool | None
    data_quality: DataQualityReport | None
    error: str | None = None


@dataclass(frozen=True)
class EntryTrigger:
    timeframe: str
    confirmed: bool | None
    regime: MarketRegime | None
    rsi: float | None
    close: float | None
    ema_50: float | None
    data_quality: DataQualityReport | None
    reason: str
    error: str | None = None


def validate_confirmation_timeframes(
    primary_timeframe: str,
    confirmation_timeframes: tuple[str, ...],
) -> tuple[str, ...]:
    primary_ms = timeframe_to_milliseconds(primary_timeframe)
    cleaned: list[str] = []

    for timeframe in confirmation_timeframes:
        timeframe = timeframe.strip()
        if not timeframe:
            continue
        if timeframe in cleaned:
            raise ValueError(
                f"Duplicate confirmation timeframe: {timeframe}"
            )
        if timeframe_to_milliseconds(timeframe) <= primary_ms:
            raise ValueError(
                "Confirmation timeframes must be higher than "
                f"the primary timeframe ({primary_timeframe})."
            )
        cleaned.append(timeframe)

    return tuple(cleaned)


def validate_entry_timeframe(
    primary_timeframe: str,
    entry_timeframe: str | None,
) -> str | None:
    if entry_timeframe is None:
        return None

    timeframe = entry_timeframe.strip()
    if not timeframe:
        return None

    if (
        timeframe_to_milliseconds(timeframe)
        >= timeframe_to_milliseconds(primary_timeframe)
    ):
        raise ValueError(
            "Entry timeframe must be lower than the primary "
            f"timeframe ({primary_timeframe})."
        )

    return timeframe


def regime_alignment(
    signal: Signal,
    regime: MarketRegime,
    strategy: str = "trend_continuation",
) -> bool | None:
    if strategy == "range_mean_reversion" and signal != Signal.NO_TRADE:
        if regime == MarketRegime.RANGE:
            return True
        if regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND):
            return False
        return None
    if signal == Signal.LONG:
        if regime == MarketRegime.BULL_TREND:
            return True
        if regime == MarketRegime.BEAR_TREND:
            return False
        return None

    if signal == Signal.SHORT:
        if regime == MarketRegime.BEAR_TREND:
            return True
        if regime == MarketRegime.BULL_TREND:
            return False
        return None

    return None


def evaluate_entry_trigger(
    signal: Signal,
    timeframe: str,
    data: pd.DataFrame,
    regime: MarketRegime,
    data_quality: DataQualityReport | None,
    strategy: str = "trend_continuation",
) -> EntryTrigger:
    required = {"close", "ema_50", "rsi_14"}
    missing = required.difference(data.columns)
    if missing or data.empty:
        return EntryTrigger(
            timeframe=timeframe,
            confirmed=None,
            regime=regime,
            rsi=None,
            close=None,
            ema_50=None,
            data_quality=data_quality,
            reason="Lower-timeframe trigger data is incomplete.",
        )

    latest = data.iloc[-1]
    values = latest[list(required)]
    if values.isna().any():
        return EntryTrigger(
            timeframe=timeframe,
            confirmed=None,
            regime=regime,
            rsi=None,
            close=None,
            ema_50=None,
            data_quality=data_quality,
            reason="Lower-timeframe trigger indicators are incomplete.",
        )

    close = float(latest["close"])
    ema_50 = float(latest["ema_50"])
    rsi = float(latest["rsi_14"])

    if strategy == "range_mean_reversion":
        # A range reversal needs a fresh directional turn, not trend agreement.
        if len(data) < 2 or not {"open", "high", "low"}.issubset(data.columns):
            confirmed = False
        else:
            previous = data.iloc[-2]
            confirmed = bool(
                close > float(latest["open"]) and close > float(previous["close"])
                and rsi > float(previous["rsi_14"]) and regime != MarketRegime.BEAR_TREND
                if signal == Signal.LONG else
                close < float(latest["open"]) and close < float(previous["close"])
                and rsi < float(previous["rsi_14"]) and regime != MarketRegime.BULL_TREND
            )
        return EntryTrigger(timeframe, confirmed, regime, rsi, close, ema_50,
                            data_quality, "Range reversal trigger confirmed." if confirmed
                            else "Waiting for a fresh range reversal trigger.")

    if signal == Signal.LONG:
        if regime == MarketRegime.BEAR_TREND:
            confirmed = False
            reason = "Lower timeframe is in an opposing bear trend."
        elif close > ema_50 and rsi > 50:
            confirmed = True
            reason = (
                "Lower timeframe confirms LONG momentum: "
                "price is above EMA50 and RSI is above 50."
            )
        else:
            confirmed = False
            reason = (
                "LONG thesis remains valid, but lower-timeframe "
                "entry momentum is not confirmed yet."
            )
    elif signal == Signal.SHORT:
        if regime == MarketRegime.BULL_TREND:
            confirmed = False
            reason = "Lower timeframe is in an opposing bull trend."
        elif close < ema_50 and rsi < 50:
            confirmed = True
            reason = (
                "Lower timeframe confirms SHORT momentum: "
                "price is below EMA50 and RSI is below 50."
            )
        else:
            confirmed = False
            reason = (
                "SHORT thesis remains valid, but lower-timeframe "
                "entry momentum is not confirmed yet."
            )
    else:
        confirmed = None
        reason = "No directional setup exists for entry confirmation."

    return EntryTrigger(
        timeframe=timeframe,
        confirmed=confirmed,
        regime=regime,
        rsi=rsi,
        close=close,
        ema_50=ema_50,
        data_quality=data_quality,
        reason=reason,
    )


def apply_confirmation_gate(
    assessment: SetupAssessment,
    confirmations: tuple[TimeframeConfirmation, ...],
) -> tuple[SetupAssessment, bool | None]:
    """Gate a primary setup without changing its rule score.

    Opposite higher-timeframe trends invalidate the setup. Range/unknown or
    unavailable context makes an otherwise confirmed setup FORMING. Fully
    aligned higher timeframes preserve the primary status.
    """
    if not confirmations or assessment.signal == Signal.NO_TRADE:
        return assessment, None

    conflicts = [
        item
        for item in confirmations
        if item.aligned is False
    ]
    incomplete = [
        item
        for item in confirmations
        if item.error or item.aligned is None
    ]

    if conflicts:
        labels = ", ".join(
            f"{item.timeframe}={item.regime.value if item.regime else 'ERROR'}"
            for item in conflicts
        )
        reason = (
            assessment.reason
            + f"; Higher-timeframe conflict: {labels}."
        )
        return (
            replace(
                assessment,
                signal=Signal.NO_TRADE,
                status=SetupStatus.NO_TRADE,
                score=assessment.score,
                reason=reason,
                reasons=assessment.reasons
                + (f"Higher-timeframe conflict: {labels}.",),
                strategy=assessment.strategy,
            ),
            False,
        )

    if incomplete:
        labels = ", ".join(
            (
                f"{item.timeframe}=UNAVAILABLE"
                if item.error
                else f"{item.timeframe}={item.regime.value if item.regime else 'UNKNOWN'}"
            )
            for item in incomplete
        )
        reason = (
            assessment.reason
            + f"; Higher-timeframe confirmation incomplete: {labels}."
        )
        status = (
            SetupStatus.FORMING
            if assessment.status == SetupStatus.CONFIRMED
            else assessment.status
        )
        return (
            replace(
                assessment,
                signal=assessment.signal,
                status=status,
                score=assessment.score,
                reason=reason,
                reasons=assessment.reasons
                + (f"Higher-timeframe confirmation incomplete: {labels}.",),
                strategy=assessment.strategy,
            ),
            None,
        )

    labels = ", ".join(
        f"{item.timeframe}={item.regime.value}"
        for item in confirmations
        if item.regime is not None
    )
    reason = (
        assessment.reason
        + f"; Higher-timeframe alignment confirmed: {labels}."
    )
    return (
        replace(
            assessment,
            signal=assessment.signal,
            status=assessment.status,
            score=assessment.score,
            reason=reason,
            reasons=assessment.reasons
            + (f"Higher-timeframe alignment confirmed: {labels}.",),
            strategy=assessment.strategy,
        ),
        True,
    )


def apply_entry_trigger_gate(
    assessment: SetupAssessment,
    trigger: EntryTrigger | None,
) -> SetupAssessment:
    if (
        trigger is None
        or assessment.signal == Signal.NO_TRADE
        or assessment.status == SetupStatus.NO_TRADE
    ):
        return assessment

    if trigger.confirmed is True:
        return replace(
            assessment,
            signal=assessment.signal,
            status=assessment.status,
            score=assessment.score,
            reason=assessment.reason + "; " + trigger.reason,
            reasons=assessment.reasons + (trigger.reason,),
            strategy=assessment.strategy,
        )

    status = (
        SetupStatus.FORMING
        if assessment.status == SetupStatus.CONFIRMED
        else assessment.status
    )
    return replace(
        assessment,
        signal=assessment.signal,
        status=status,
        score=assessment.score,
        reason=assessment.reason + "; " + trigger.reason,
        reasons=assessment.reasons + (trigger.reason,),
        strategy=assessment.strategy,
    )
