from trading_bot.core.enums import Signal
from trading_bot.risk.models import TradePlan


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

    if entry_price <= 0:
        raise ValueError("Entry price must be greater than zero.")

    if atr <= 0:
        raise ValueError("ATR must be greater than zero.")

    if account_equity <= 0:
        raise ValueError("Account equity must be greater than zero.")

    if not 0 < risk_percent <= 1:
        raise ValueError("Risk percent must be between 0 and 1.")

    risk_amount = account_equity * risk_percent

    stop_distance = atr * atr_stop_multiplier

    position_size = risk_amount / stop_distance

    max_notional = account_equity * max_notional_percent
    calculated_notional = position_size * entry_price

    if calculated_notional > max_notional:
        position_size = max_notional / entry_price

    notional_value = position_size * entry_price

    if signal == Signal.LONG:
        stop_loss = entry_price - stop_distance
        take_profit = (
            entry_price
            + stop_distance * reward_to_risk
        )

    elif signal == Signal.SHORT:
        stop_loss = entry_price + stop_distance
        take_profit = (
            entry_price
            - stop_distance * reward_to_risk
        )

    else:
        return None

    return TradePlan(
        signal=signal,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size=position_size,
        risk_amount=risk_amount,
        stop_distance=stop_distance,
        notional_value=notional_value,
    )