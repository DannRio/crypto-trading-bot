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
    event_id: str | None = None
    setup_type: str = "legacy_state"
    event_time: str | None = None
    anchor_price: float | None = None
    invalidation_price: float | None = None
    target_price: float | None = None
    cancel_price: float | None = None
