from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSizeResult:
    position_size: float
    target_risk_amount: float
    actual_risk_amount: float
    stop_distance: float
    notional_value: float
