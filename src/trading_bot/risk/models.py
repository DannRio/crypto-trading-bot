from dataclasses import dataclass

from trading_bot.core.enums import Signal


@dataclass(frozen=True)
class PositionSizeResult:
    position_size: float
    target_risk_amount: float
    actual_risk_amount: float
    stop_distance: float
    notional_value: float


@dataclass(frozen=True)
class TradePlan:
    signal: Signal
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    risk_amount: float
    stop_distance: float
    notional_value: float
