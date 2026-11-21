from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any


class MarketRegime(str, Enum):
    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    RANGE = "range"
    TRANSITION = "transition"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"


class SignalAction(str, Enum):
    LONG = "long"
    SHORT = "short"
    WATCH_LONG = "watch_long"
    WATCH_SHORT = "watch_short"
    NO_TRADE = "no_trade"


class SetupType(str, Enum):
    BREAKOUT = "breakout"
    PULLBACK = "pullback"
    NONE = "none"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    value: float
    explanation: str


@dataclass(frozen=True)
class TimeframeState:
    timeframe: str
    candle_time: datetime
    regime: MarketRegime
    score: float
    confidence: float
    adx: float
    rsi: float
    atr: float
    atr_percent: float
    volatility_percentile: float
    volume_ratio: float
    distance_from_fast_ema_atr: float
    overextended: bool
    score_components: tuple[ScoreComponent, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EntryPlan:
    side: PositionSide
    setup_type: SetupType
    trigger_price: float
    entry_low: float
    entry_high: float
    reference_entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    risk_per_unit: float
    target_risk_amount: float
    actual_risk_amount: float
    position_size: float
    notional_value: float
    estimated_round_trip_cost: float
    reward_to_risk_1: float
    reward_to_risk_2: float
    reward_to_risk_3: float
    move_stop_to_break_even_after: float
    trailing_stop_after: float
    trailing_atr_multiple: float
    max_holding_bars: int


@dataclass(frozen=True)
class MarketSignal:
    symbol: str
    generated_at: datetime
    execution_timeframe: str
    action: SignalAction
    regime: MarketRegime
    score: float
    confidence: float
    consensus: float
    setup_type: SetupType
    trigger_price: float | None
    entry_plan: EntryPlan | None
    timeframe_states: tuple[TimeframeState, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        generated_at: datetime,
        execution_timeframe: str,
        action: SignalAction,
        regime: MarketRegime,
        score: float,
        confidence: float,
        consensus: float,
        setup_type: SetupType,
        trigger_price: float | None,
        entry_plan: EntryPlan | None,
        timeframe_states: tuple[TimeframeState, ...],
        reasons: tuple[str, ...],
        warnings: tuple[str, ...],
    ) -> "MarketSignal":
        raw = "|".join(
            [
                symbol,
                generated_at.isoformat(),
                execution_timeframe,
                action.value,
                regime.value,
                f"{score:.4f}",
                f"{trigger_price:.10f}" if trigger_price is not None else "none",
            ]
        )
        return cls(
            symbol=symbol,
            generated_at=generated_at,
            execution_timeframe=execution_timeframe,
            action=action,
            regime=regime,
            score=score,
            confidence=confidence,
            consensus=consensus,
            setup_type=setup_type,
            trigger_price=trigger_price,
            entry_plan=entry_plan,
            timeframe_states=timeframe_states,
            reasons=reasons,
            warnings=warnings,
            fingerprint=sha256(raw.encode("utf-8")).hexdigest()[:20],
        )


@dataclass(frozen=True)
class BacktestTrade:
    side: PositionSide
    setup_type: SetupType
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    initial_stop: float
    quantity: float
    gross_pnl: float
    fees: float
    net_pnl: float
    initial_risk: float
    r_multiple: float
    exit_reason: str
    bars_held: int


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    execution_timeframe: str
    starting_equity: float
    ending_equity: float
    net_pnl: float
    total_return_percent: float
    max_drawdown_percent: float
    trades: int
    wins: int
    losses: int
    win_rate_percent: float
    profit_factor: float | None
    expectancy_r: float
    average_r: float
    trades_detail: tuple[BacktestTrade, ...]
    equity_curve: tuple[tuple[datetime, float], ...]


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    return value


def to_dict(value: Any) -> dict[str, Any]:
    serialized = _serialize(value)
    if not isinstance(serialized, dict):
        raise TypeError("Expected a dataclass or dictionary-like value.")
    return serialized
