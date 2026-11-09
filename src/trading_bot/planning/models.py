from dataclasses import dataclass

from trading_bot.core.enums import Signal, SetupStatus


@dataclass(frozen=True)
class TradePlanConfig:
    atr_stop_multiplier: float = 2.0
    structure_buffer_atr: float = 0.25
    entry_zone_atr: float = 0.25
    max_structure_stop_atr: float = 3.0
    max_notional_percent: float = 1.0
    reward_to_risk_1: float = 1.0
    reward_to_risk_2: float = 2.0
    take_profit_1_fraction: float = 0.5
    move_stop_to_break_even_after_tp1: bool = True

    def __post_init__(self):
        positive = {
            "atr_stop_multiplier": self.atr_stop_multiplier,
            "structure_buffer_atr": self.structure_buffer_atr,
            "entry_zone_atr": self.entry_zone_atr,
            "max_structure_stop_atr": self.max_structure_stop_atr,
            "max_notional_percent": self.max_notional_percent,
            "reward_to_risk_1": self.reward_to_risk_1,
            "reward_to_risk_2": self.reward_to_risk_2,
        }
        invalid = [
            name
            for name, value in positive.items()
            if value <= 0
        ]
        if invalid:
            raise ValueError(
                "Trade-plan parameters must be positive: "
                + ", ".join(invalid)
            )
        if self.reward_to_risk_2 < self.reward_to_risk_1:
            raise ValueError(
                "reward_to_risk_2 must be >= reward_to_risk_1."
            )
        if not 0 <= self.take_profit_1_fraction < 1:
            raise ValueError(
                "take_profit_1_fraction must be >= 0 and < 1."
            )


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
    take_profit_1_fraction: float = 0.5
    move_stop_to_break_even_after_tp1: bool = True
