"""Shared cost-aware trade-plan validation for reports, scanning and simulation."""
from dataclasses import asdict, is_dataclass
import math


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def revalidate_trade_plan(proposed, *, account_equity, risk_percent, notional_cap,
                          fee_rate, slippage_bps, market_type="spot", liquidity=None,
                          market=None, min_target_net_rr=1.0, min_blended_net_rr=1.0):
    """Return the cost-capped plan dictionary and explicit rejection reasons.

    Risk is a fraction of equity; notional_cap is an absolute quote-currency
    ceiling. Price levels retain their original structural invalidation. A
    current book may raise the cost assumption but can never lower it.
    """
    liquidity = liquidity or {}
    market = market or {}
    if is_dataclass(proposed):
        proposed = asdict(proposed)
    if not all(_finite(value) for value in (account_equity, risk_percent, notional_cap,
                                           fee_rate, slippage_bps, min_target_net_rr, min_blended_net_rr)):
        return None, ["Trade-plan assumptions must be finite."]
    if (account_equity <= 0 or not 0 < risk_percent <= 1 or notional_cap <= 0
            or not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10000
            or min_target_net_rr < 0 or min_blended_net_rr < 0):
        return None, ["Trade-plan assumptions are outside their valid ranges."]
    if proposed is None:
        return None, []
    if not isinstance(proposed, dict):
        return None, ["The strategy returned an invalid trade plan."]
    plan = dict(proposed)
    names = ("reference_entry", "entry_low", "entry_high", "stop_loss", "take_profit_1", "take_profit_2", "position_size")
    if (not all(_finite(plan.get(name)) and plan[name] > 0 for name in names)
            or plan.get("signal") not in {"LONG", "SHORT"} or plan.get("status") != "CONFIRMED"):
        return None, ["The strategy returned an invalid price or size."]
    direction = 1 if plan["signal"] == "LONG" else -1
    risk_edge = plan["entry_low"] if direction == 1 else plan["entry_high"]
    target_edge = plan["entry_high"] if direction == 1 else plan["entry_low"]
    if (not plan["entry_low"] <= plan["reference_entry"] <= plan["entry_high"]
            or direction * (risk_edge - plan["stop_loss"]) <= 0
            or direction * (plan["take_profit_1"] - target_edge) <= 0
            or direction * (plan["take_profit_2"] - plan["take_profit_1"]) < 0):
        return None, ["The entry zone, invalidation and targets have inconsistent directional geometry."]
    fraction = plan.get("take_profit_1_fraction", 0.5)
    if not _finite(fraction) or not 0 <= fraction < 1:
        return None, ["The first-target exit allocation must be finite and between zero and one, excluding one."]
    cancel = plan.get("cancel_price")
    if cancel is not None and (not _finite(cancel) or cancel <= 0):
        return None, ["The structural cancellation price is invalid."]
    observed_slip = [(liquidity.get(side) or {}).get("slippage_bps") for side in ("buy_fill", "sell_fill")]
    slip_bps = max([slippage_bps] + [value for value in observed_slip if _finite(value) and value >= 0])
    slip = slip_bps / 10_000
    entry = (plan["entry_high"] if direction == 1 else plan["entry_low"]) * (1 + direction * slip)
    stop = plan["stop_loss"] * (1 - direction * slip)
    loss = direction * (entry - stop) + fee_rate * (entry + stop)
    gains = []
    for target in (plan["take_profit_1"], plan["take_profit_2"]):
        fill = target * (1 - direction * slip)
        gains.append(direction * (fill - entry) - fee_rate * (entry + fill))
    # A short's worst risk entry is the low end, but its largest possible
    # notional is at the high end.  Cap both directions over the whole zone.
    notional_ceiling = plan["entry_high"] * (1 + slip)
    notional_floor = plan["entry_low"] * (1 - slip)
    if (loss <= 0 or not all(math.isfinite(v) for v in [entry, stop, loss, notional_ceiling, notional_floor, *gains])
            or min(entry, stop, notional_ceiling, notional_floor) <= 0):
        return None, ["The cost-adjusted trade geometry is invalid."]
    budget = min(notional_cap, account_equity)
    quantity = min(plan["position_size"], account_equity * risk_percent / loss,
                   budget / (notional_ceiling * (1 + fee_rate)))
    if not _finite(quantity) or quantity <= 0:
        return None, ["No finite positive position fits the risk and notional caps."]
    errors = []
    blended_rr = (fraction * gains[0] + (1 - fraction) * gains[1]) / loss
    target_rr = gains[1] / loss
    if not all(_finite(value) for value in (target_rr, blended_rr)):
        return None, ["The cost-adjusted reward estimate is outside the supported numeric range."]
    if gains[0] <= 0 or target_rr < min_target_net_rr or blended_rr < min_blended_net_rr:
        errors.append("Reward after modeled fees and slippage does not meet the configured net reward/risk minimum.")
    units = quantity
    if market_type == "swap" or market.get("contract") is True:
        multiplier = market.get("contractSize")
        if not _finite(multiplier) or multiplier <= 0:
            return None, ["The contract quantity multiplier is invalid."]
        units = quantity / multiplier
        if not _finite(units) or units <= 0:
            return None, ["The proposed contract count is outside the supported numeric range."]
    limits = market.get("limits") or {}
    if not isinstance(limits, dict):
        return None, ["Exchange order limits are malformed."]
    for name, minimum_value, maximum_value in (("amount", units, units),
            ("cost", quantity * notional_floor, quantity * notional_ceiling)):
        limit = limits.get(name) or {}
        if not isinstance(limit, dict) or any(
                value is not None and (not _finite(value) or value < 0)
                for value in (limit.get("min"), limit.get("max"))):
            return None, [f"The exchange {name} limits are malformed."]
        if _finite(limit.get("min")) and minimum_value < limit["min"]:
            errors.append(f"The risk-capped position is below the exchange minimum {name}.")
        if _finite(limit.get("max")) and maximum_value > limit["max"]:
            errors.append(f"The position exceeds the exchange maximum {name}.")
    distance = direction * (plan["reference_entry"] - plan["stop_loss"])
    plan.update(position_size=quantity, position_size_base=quantity,
                notional_value=quantity * plan["reference_entry"],
                reference_price_risk_amount=quantity * distance,
                actual_risk_amount=quantity * loss, estimated_loss_at_stop=quantity * loss,
                target_risk_amount=account_equity * risk_percent,
                reward_to_risk_1=direction * (plan["take_profit_1"] - plan["reference_entry"]) / distance,
                reward_to_risk_2=direction * (plan["take_profit_2"] - plan["reference_entry"]) / distance,
                net_reward_to_risk=target_rr,
                blended_target_net_reward_to_risk=blended_rr, contract_count_estimate=units if market_type == "swap" else None,
                execution_supported=False, exchange_precision_applied=False,
                entry_condition="All gates pass, the report is unexpired, and the executable price remains inside the entry zone.",
                cancel_conditions=["Price reaches structural invalidation or a target before entry.",
                                   "The report expires, a new 1h decision is due, or data/liquidity gates fail.",
                                   "Price leaves the entry zone; wait for a fresh analysis instead of chasing."],
                costs={"fee_rate_per_side": fee_rate, "slippage_bps_per_side": slip_bps,
                       "requested_slippage_bps": slippage_bps, "worst_modeled_entry": entry,
                       "modeled_stop_fill": stop, "loss_per_base_unit": loss,
                       "notional_cap": budget, "maximum_entry_notional": quantity * notional_ceiling,
                       "estimated_loss_at_stop": quantity * loss,
                       "limitations": ["Fees are user assumptions, not verified account fee tiers.",
                                       "Depth is one snapshot; stop gaps, future slippage and funding can increase losses.",
                                       "Size is an estimate in base units; exchange amount precision is not applied."]})
    return plan, errors
