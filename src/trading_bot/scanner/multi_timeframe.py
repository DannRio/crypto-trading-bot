from dataclasses import dataclass

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


def regime_alignment(
    signal: Signal,
    regime: MarketRegime,
) -> bool | None:
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
            SetupAssessment(
                signal=Signal.NO_TRADE,
                status=SetupStatus.NO_TRADE,
                score=assessment.score,
                reason=reason,
                reasons=assessment.reasons
                + (f"Higher-timeframe conflict: {labels}.",),
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
            SetupAssessment(
                signal=assessment.signal,
                status=status,
                score=assessment.score,
                reason=reason,
                reasons=assessment.reasons
                + (f"Higher-timeframe confirmation incomplete: {labels}.",),
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
        SetupAssessment(
            signal=assessment.signal,
            status=assessment.status,
            score=assessment.score,
            reason=reason,
            reasons=assessment.reasons
            + (f"Higher-timeframe alignment confirmed: {labels}.",),
        ),
        True,
    )
