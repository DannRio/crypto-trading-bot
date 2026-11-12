from trading_bot.risk.models import PositionSizeResult


def size_position_for_stop(
    entry_price: float,
    stop_loss: float,
    account_equity: float,
    risk_percent: float = 0.01,
    max_notional_percent: float = 1.0,
) -> PositionSizeResult:
    if entry_price <= 0:
        raise ValueError("Entry price must be greater than zero.")
    if stop_loss <= 0:
        raise ValueError("Stop loss must be greater than zero.")
    if account_equity <= 0:
        raise ValueError("Account equity must be greater than zero.")
    if not 0 < risk_percent <= 1:
        raise ValueError("Risk percent must be between 0 and 1.")
    if max_notional_percent <= 0:
        raise ValueError("Max notional percent must be greater than zero.")

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        raise ValueError("Stop loss must differ from entry price.")

    target_risk_amount = account_equity * risk_percent
    position_size = target_risk_amount / stop_distance
    max_notional = account_equity * max_notional_percent
    if position_size * entry_price > max_notional:
        position_size = max_notional / entry_price

    notional_value = position_size * entry_price
    actual_risk_amount = position_size * stop_distance
    return PositionSizeResult(
        position_size=position_size,
        target_risk_amount=target_risk_amount,
        actual_risk_amount=actual_risk_amount,
        stop_distance=stop_distance,
        notional_value=notional_value,
    )
