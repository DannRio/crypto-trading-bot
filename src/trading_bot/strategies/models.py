from dataclasses import dataclass

from trading_bot.core.enums import Signal, SetupStatus


@dataclass(frozen=True)
class SetupAssessment:
    signal: Signal
    status: SetupStatus
    score: int
    reason: str
    reasons: tuple[str, ...]
    strategy: str = "trend_continuation"
