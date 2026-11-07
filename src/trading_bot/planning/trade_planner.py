import math

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.planning.models import TradeSetup
from trading_bot.risk import size_position_for_stop


def create_trade_setup(
    signal: Signal,
    status: SetupStatus,
    score: int,
    reasons: tuple[str, ...],
    entry_price: float,
    atr: float,
    structure: MarketStructure,
    account_equity: float,
    risk_percent: float = 0.01,
    atr_stop_multiplier: float = 2.0,
    structure_buffer_atr: float = 0.25,
    entry_zone_atr: float = 0.25,
    max_structure_stop_atr: float = 3.0,
    max_notional_percent: float = 1.0,
) -> TradeSetup | None:
    if signal == Signal.NO_TRADE or status != SetupStatus.CONFIRMED:
        return None
    if entry_price <= 0 or atr <= 0:
        raise ValueError("Entry price and ATR must be positive.")

    atr_stop_distance = atr * atr_stop_multiplier
    structure_buffer = atr * structure_buffer_atr

    if signal == Signal.LONG:
        stop_loss = entry_price - atr_stop_distance
        structural_stop = structure.support - structure_buffer
        if (
            math.isfinite(structural_stop)
            and structural_stop < entry_price
            and (entry_price - structural_stop) <= (atr * max_structure_stop_atr)
        ):
            stop_loss = min(stop_loss, structural_stop)
        entry_low = entry_price - (atr * entry_zone_atr)
        entry_high = entry_price + (atr * 0.10)
    elif signal == Signal.SHORT:
        stop_loss = entry_price + atr_stop_distance
        structural_stop = structure.resistance + structure_buffer
        if (
            math.isfinite(structural_stop)
            and structural_stop > entry_price
            and (structural_stop - entry_price) <= (atr * max_structure_stop_atr)
        ):
            stop_loss = max(stop_loss, structural_stop)
        entry_low = entry_price - (atr * 0.10)
        entry_high = entry_price + (atr * entry_zone_atr)
    else:
        return None

    sizing = size_position_for_stop(
        entry_price=entry_price,
        stop_loss=stop_loss,
        account_equity=account_equity,
        risk_percent=risk_percent,
        max_notional_percent=max_notional_percent,
    )
    risk_per_unit = sizing.stop_distance

    if signal == Signal.LONG:
        take_profit_1 = entry_price + risk_per_unit
        take_profit_2 = entry_price + (2 * risk_per_unit)
    else:
        take_profit_1 = entry_price - risk_per_unit
        take_profit_2 = entry_price - (2 * risk_per_unit)

    return TradeSetup(
        signal=signal,
        status=status,
        score=score,
        reference_entry=entry_price,
        entry_low=min(entry_low, entry_high),
        entry_high=max(entry_low, entry_high),
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        reward_to_risk_1=1.0,
        reward_to_risk_2=2.0,
        position_size=sizing.position_size,
        notional_value=sizing.notional_value,
        target_risk_amount=sizing.target_risk_amount,
        actual_risk_amount=sizing.actual_risk_amount,
        reasons=reasons,
    )
