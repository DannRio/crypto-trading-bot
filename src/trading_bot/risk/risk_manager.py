from trading_bot.core.enums import Signal
from trading_bot.risk.models import PositionSizeResult, TradePlan


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


def create_trade_plan(
    signal: Signal,
    entry_price: float,
    atr: float,
    account_equity: float,
    risk_percent: float = 0.01,
    atr_stop_multiplier: float = 2.0,
    reward_to_risk: float = 2.0,
    max_notional_percent: float = 1.0,
) -> TradePlan | None:
    if signal == Signal.NO_TRADE:
        return None
    if atr <= 0:
        raise ValueError("ATR must be greater than zero.")
    if atr_stop_multiplier <= 0:
        raise ValueError("ATR stop multiplier must be greater than zero.")
    if reward_to_risk <= 0:
        raise ValueError("Reward-to-risk must be greater than zero.")

    stop_distance = atr * atr_stop_multiplier
    if signal == Signal.LONG:
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + stop_distance * reward_to_risk
    elif signal == Signal.SHORT:
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - stop_distance * reward_to_risk
    else:
        return None

    sizing = size_position_for_stop(
        entry_price=entry_price,
        stop_loss=stop_loss,
        account_equity=account_equity,
        risk_percent=risk_percent,
        max_notional_percent=max_notional_percent,
    )
    return TradePlan(
        signal=signal,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size=sizing.position_size,
        risk_amount=sizing.actual_risk_amount,
        stop_distance=sizing.stop_distance,
        notional_value=sizing.notional_value,
    )
