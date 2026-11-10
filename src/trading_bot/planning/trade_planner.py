import math

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.planning.models import TradePlanConfig, TradeSetup
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
    config: TradePlanConfig | None = None,
    event_id: str | None = None,
    setup_type: str = "legacy_state",
    anchor_price: float | None = None,
    invalidation_price: float | None = None,
    target_price: float | None = None,
    cancel_price: float | None = None,
    event_time: str | None = None,
    fee_rate: float = 0.0,
    slippage_bps: float = 0.0,
) -> TradeSetup | None:
    if signal == Signal.NO_TRADE or status != SetupStatus.CONFIRMED:
        return None
    if not all(math.isfinite(x) for x in (entry_price, atr, fee_rate, slippage_bps)) or entry_price <= 0 or atr <= 0:
        raise ValueError("Entry price and ATR must be positive.")

    if not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10000:
        raise ValueError("Invalid execution cost assumptions.")
    config = config or TradePlanConfig()
    atr_stop_distance = atr * config.atr_stop_multiplier
    structure_buffer = atr * config.structure_buffer_atr

    if signal == Signal.LONG:
        stop_loss = entry_price - atr_stop_distance
        structural_stop = structure.support - structure_buffer
        if (
            math.isfinite(structural_stop)
            and structural_stop < entry_price
            and (entry_price - structural_stop) <= (atr * config.max_structure_stop_atr)
        ):
            stop_loss = min(stop_loss, structural_stop)
        entry_low = entry_price - (atr * config.entry_zone_atr)
        entry_high = entry_price + (atr * 0.10)
    elif signal == Signal.SHORT:
        stop_loss = entry_price + atr_stop_distance
        structural_stop = structure.resistance + structure_buffer
        if (
            math.isfinite(structural_stop)
            and structural_stop > entry_price
            and (structural_stop - entry_price) <= (atr * config.max_structure_stop_atr)
        ):
            stop_loss = max(stop_loss, structural_stop)
        entry_low = entry_price - (atr * 0.10)
        entry_high = entry_price + (atr * config.entry_zone_atr)
    else:
        return None

    direction = 1 if signal == Signal.LONG else -1
    if invalidation_price is not None:
        # Preserve the setup's actual invalidation. A far stop rejects the trade;
        # it is never replaced with a closer, cosmetically attractive ATR stop.
        structural_distance = direction * (entry_price - invalidation_price)
        if (not math.isfinite(invalidation_price) or invalidation_price <= 0
                or structural_distance <= 0
                or structural_distance > atr * config.max_structure_stop_atr):
            return None
        stop_loss = float(invalidation_price)

    sizing = size_position_for_stop(
        entry_price=entry_price,
        stop_loss=stop_loss,
        account_equity=account_equity,
        risk_percent=risk_percent,
        max_notional_percent=config.max_notional_percent,
    )
    risk_per_unit = sizing.stop_distance

    if signal == Signal.LONG:
        take_profit_1 = (
            entry_price
            + config.reward_to_risk_1 * risk_per_unit
        )
        take_profit_2 = (
            entry_price
            + config.reward_to_risk_2 * risk_per_unit
        )
    else:
        take_profit_1 = (
            entry_price
            - config.reward_to_risk_1 * risk_per_unit
        )
        take_profit_2 = (
            entry_price
            - config.reward_to_risk_2 * risk_per_unit
        )

    if target_price is not None and config.target_mode == "auto":
        take_profit_2 = float(target_price)
        # Range targets remain inside the opposing boundary. TP1 is the range midpoint.
        take_profit_1 = ((float(anchor_price) + take_profit_2) / 2
                         if anchor_price is not None else (entry_price + take_profit_2) / 2)
    if (min(stop_loss, take_profit_1, take_profit_2) <= 0
            or direction * (take_profit_1 - entry_price) <= 0
            or direction * (take_profit_2 - take_profit_1) < 0):
        return None

    # Use the worst admissible entry, not a flattering reference fill.
    slip = slippage_bps / 10000
    worst_entry = (entry_high if direction == 1 else entry_low) * (1 + direction * slip)
    stop_fill = stop_loss * (1 - direction * slip)
    target_fill = take_profit_2 * (1 - direction * slip)
    net_loss = direction * (worst_entry - stop_fill) + fee_rate * (worst_entry + stop_fill)
    net_gain = direction * (target_fill - worst_entry) - fee_rate * (worst_entry + target_fill)
    net_rr = net_gain / net_loss if net_loss > 0 else 0.0
    quantity = sizing.position_size
    if event_id is not None:
        if net_rr < config.min_target_net_rr:
            return None
        quantity = min(quantity, sizing.target_risk_amount / net_loss,
                       account_equity * config.max_notional_percent / (worst_entry * (1 + fee_rate)))
    risk_amount = abs(entry_price - stop_loss) * quantity

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
        reward_to_risk_1=direction * (take_profit_1 - entry_price) / risk_per_unit,
        reward_to_risk_2=direction * (take_profit_2 - entry_price) / risk_per_unit,
        position_size=quantity,
        notional_value=entry_price * quantity,
        target_risk_amount=sizing.target_risk_amount,
        actual_risk_amount=risk_amount,
        reasons=reasons,
        event_id=event_id, setup_type=setup_type, anchor_price=anchor_price,
        cancel_price=cancel_price, event_time=event_time,
        break_even_mode=config.break_even_mode, net_reward_to_risk=net_rr,
        take_profit_1_fraction=config.take_profit_1_fraction,
        move_stop_to_break_even_after_tp1=(
            config.move_stop_to_break_even_after_tp1
        ),
    )
