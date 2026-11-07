from dataclasses import dataclass

from trading_bot.core.enums import Signal, SetupStatus


@dataclass(frozen=True)
class TradeSetup:
    signal: Signal
    status: SetupStatus
    score: int
    reference_entry: float
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    reward_to_risk_1: float
    reward_to_risk_2: float
    position_size: float
    notional_value: float
    target_risk_amount: float
    actual_risk_amount: float
    reasons: tuple[str, ...]
