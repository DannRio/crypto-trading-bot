from dataclasses import asdict
from copy import deepcopy
import math
from types import SimpleNamespace
from trading_bot.core.candle_execution import replay_price_path, economic_break_even, ohlc_paths

import pandas as pd

from trading_bot.analysis import add_indicators
from trading_bot.backtesting.metrics import (
    calculate_return_pct,
    calculate_trade_metrics,
)
from trading_bot.backtesting.models import BacktestResult, CompletedTrade
from trading_bot.backtesting.timeframe_gates import HistoricalTimeframeGates
from trading_bot.data import timeframe_to_milliseconds, validate_ohlcv
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.decision import DecisionEngine
from trading_bot.planning import TradePlanConfig
from trading_bot.strategies import DEFAULT_STRATEGIES


def _entry_with_slippage(
    price: float,
    signal: Signal,
    slippage_bps: float,
) -> float:
    slip = slippage_bps / 10_000
    return price * (1 + slip) if signal == Signal.LONG else price * (1 - slip)


def _exit_with_slippage(
    price: float,
    signal: Signal,
    slippage_bps: float,
) -> float:
    slip = slippage_bps / 10_000
    return price * (1 - slip) if signal == Signal.LONG else price * (1 + slip)


def _gross_pnl(
    signal: Signal,
    entry_price: float,
    exit_price: float,
    position_size: float,
) -> float:
    if signal == Signal.LONG:
        return (exit_price - entry_price) * position_size
    return (entry_price - exit_price) * position_size


def _entry_price_from_zone(candle, setup) -> float | None:
    """Return a conservative raw fill when an OHLC candle overlaps the zone."""
    low = float(candle["low"])
    high = float(candle["high"])
    zone_low = float(setup.entry_low)
    zone_high = float(setup.entry_high)

    if high < zone_low or low > zone_high:
        return None

    open_price = float(candle["open"])
    return min(max(open_price, zone_low), zone_high)


def _same_pending_thesis(pending, decision) -> bool:
    assessment = decision.assessment
    return (
        assessment.status == SetupStatus.CONFIRMED
        and decision.setup is not None
        and assessment.signal == pending["setup"].signal
        and assessment.strategy == pending["assessment"].strategy
    )


def run_decision_backtest(
    data: pd.DataFrame,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    min_confirmed_score: int = 70,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    warmup_candles: int = 220,
    entry_valid_bars: int = 3,
    decision_lookback: int = 250,
    allow_short: bool = True,
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
    strategy_parameters: dict[str, dict] | None = None,
    trade_plan_config: TradePlanConfig | None = None,
    strategy_plan_configs: dict[str, TradePlanConfig] | None = None,
    primary_timeframe: str = "1h",
    confirmation_timeframes: tuple[str, ...] = (),
    entry_timeframe: str | None = None,
    secondary_data: dict[str, pd.DataFrame] | None = None,
    execution_timeframe: str | None = None,
    deduplicate_setups: bool = False,
    ambiguity_policy: str = "conservative",
    cost_aware_sizing: bool = True,
) -> BacktestResult:
    """Backtest the same scored decision path used by MarketScanner.

    Confirmed setups create resting entry zones for up to entry_valid_bars.
    Pending setups are revalidated at each candle close before they can carry
    into another candle. TP1 can realize a configured partial position and,
    optionally, move the remaining stop to break-even. Same-candle ambiguity
    remains conservative: an active stop wins over a target.
    """
    if ambiguity_policy not in {"conservative", "optimistic"}:
        raise ValueError("ambiguity_policy must be conservative or optimistic.")
    if (not all(math.isfinite(x) for x in (initial_equity, risk_percent, fee_rate, slippage_bps))
            or not 0 < risk_percent <= 1 or not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10000):
        raise ValueError("Invalid risk or execution cost assumptions.")
    data = data.copy()
    quality = validate_ohlcv(data, timeframe=primary_timeframe)
    if not quality.valid or quality.missing_intervals:
        raise ValueError("Invalid primary candles: " + "; ".join(quality.issues + quality.warnings))
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive.")
    if entry_valid_bars <= 0:
        raise ValueError("entry_valid_bars must be positive.")
    if decision_lookback < 21:
        raise ValueError("decision_lookback must be at least 21 candles.")

    historical_gates = HistoricalTimeframeGates(
        primary_timeframe=primary_timeframe,
        secondary_data=secondary_data or {},
        confirmation_timeframes=confirmation_timeframes,
        entry_timeframe=entry_timeframe,
    )
    prepared = add_indicators(data.copy())
    execution = None
    execution_times = None
    primary_delta = pd.to_timedelta(
        timeframe_to_milliseconds(primary_timeframe), unit="ms"
    )
    if execution_timeframe:
        if timeframe_to_milliseconds(execution_timeframe) >= timeframe_to_milliseconds(primary_timeframe):
            raise ValueError("Execution timeframe must be lower than primary timeframe.")
        if timeframe_to_milliseconds(primary_timeframe) % timeframe_to_milliseconds(execution_timeframe):
            raise ValueError("Execution candles must divide the primary timeframe exactly.")
        if not secondary_data or execution_timeframe not in secondary_data:
            raise ValueError("Missing execution timeframe historical data.")
        execution = secondary_data[execution_timeframe].copy()
        execution["timestamp"] = pd.to_datetime(execution["timestamp"], utc=True)
        execution = execution.sort_values("timestamp").reset_index(drop=True)
        check = validate_ohlcv(execution, timeframe=execution_timeframe, min_rows=1)
        if not check.valid:
            raise ValueError("Invalid execution timeframe candles: " + "; ".join(check.issues))
        execution_times = execution["timestamp"]
    equity = initial_equity
    peak_equity = equity
    max_drawdown = 0.0

    trades: list[CompletedTrade] = []
    pending = None
    position = None
    active_thesis = None
    seen_events = set()
    execution_diagnostics = {"ambiguous_bars": 0, "pending_created": 0,
                             "pending_cancelled": 0, "pending_expired": 0,
                             "ambiguity_policy": ambiguity_policy,
                             "path_model": "OHLC extrema + TP1 reversal; sensitivity, not tick reconstruction"}
    equity_curve = []
    effective_dedupe = deduplicate_setups or "trend_continuation_v2" in strategy_names
    start_index = max(warmup_candles, 1)

    def evaluate_at(index: int):
        history = prepared.iloc[
            max(0, index - decision_lookback + 1): index + 1
        ]
        decision = DecisionEngine(
            account_equity=equity,
            risk_percent=risk_percent,
            min_confirmed_score=min_confirmed_score,
            strategy_names=strategy_names,
            strategy_parameters=strategy_parameters,
            trade_plan_config=trade_plan_config,
            strategy_plan_configs=strategy_plan_configs,
            allow_short=allow_short,
            candidate_gate=lambda assessment: historical_gates.apply_assessment(
                assessment, prepared.iloc[index]["timestamp"]),
            fee_rate=fee_rate, slippage_bps=slippage_bps,
        ).evaluate(history)
        return decision

    def close_position(
        candle,
        raw_exit_price: float,
        reason: str,
    ) -> None:
        nonlocal equity, position

        setup = position["setup"]
        remaining_size = position["remaining_size"]
        executed_exit = _exit_with_slippage(
            raw_exit_price,
            setup.signal,
            slippage_bps,
        )
        gross_pnl = _gross_pnl(
            setup.signal,
            position["entry_price"],
            executed_exit,
            remaining_size,
        )
        exit_fee = (
            executed_exit
            * remaining_size
            * fee_rate
        )
        equity += gross_pnl - exit_fee

        total_gross = (
            position["realized_gross_pnl"]
            + gross_pnl
        )
        total_slippage_cost = (
            position["entry_slippage_cost"]
            + position["realized_exit_slippage_cost"]
            + abs(executed_exit - raw_exit_price) * remaining_size
        )
        total_exit_fees = (
            position["realized_exit_fees"]
            + exit_fee
        )
        net_pnl = (
            total_gross
            - position["entry_fee"]
            - total_exit_fees
        )

        initial_size = position["initial_size"]
        weighted_exit_value = (
            position["realized_exit_value"]
            + executed_exit * remaining_size
        )
        average_exit = (
            weighted_exit_value / initial_size
            if initial_size > 0
            else executed_exit
        )

        risk_amount = position["initial_risk_amount"]
        r_multiple = (
            net_pnl / risk_amount
            if risk_amount > 0
            else 0.0
        )

        trades.append(
            CompletedTrade(
                signal=setup.signal.value,
                signal_time=position["signal_time"],
                entry_time=position["entry_time"],
                exit_time=candle["timestamp"],
                entry_price=position["entry_price"],
                exit_price=average_exit,
                stop_loss=setup.stop_loss,
                take_profit=setup.take_profit_2,
                position_size=initial_size,
                pnl=net_pnl,
                exit_reason=reason,
                strategy=position["strategy"],
                score=position["score"],
                regime=position["regime"],
                risk_amount=risk_amount,
                r_multiple=r_multiple,
                tp1_hit=position["tp1_hit"],
                gross_pnl=total_gross,
                entry_fee=position["entry_fee"],
                exit_fees=total_exit_fees,
                gross_r=(total_gross / risk_amount if risk_amount > 0 else 0.0),
                fees_r=((position["entry_fee"] + total_exit_fees) / risk_amount if risk_amount > 0 else 0.0),
                raw_gross_pnl=total_gross + total_slippage_cost,
                slippage_cost=total_slippage_cost,
                slippage_r=(total_slippage_cost / risk_amount if risk_amount > 0 else 0.0),
                event_id=setup.event_id, setup_type=setup.setup_type,
                ambiguous_bars=position["ambiguous_bars"],
                mfe_r=position["mfe_r"], mae_r=position["mae_r"],
            )
        )
        position = None

    def take_tp1() -> None:
        nonlocal equity

        setup = position["setup"]
        fraction = setup.take_profit_1_fraction
        if fraction <= 0 or position["tp1_hit"]:
            return

        exit_size = min(
            position["initial_size"] * fraction,
            position["remaining_size"],
        )
        if exit_size <= 0:
            return

        executed_exit = _exit_with_slippage(
            setup.take_profit_1,
            setup.signal,
            slippage_bps,
        )
        gross_pnl = _gross_pnl(
            setup.signal,
            position["entry_price"],
            executed_exit,
            exit_size,
        )
        exit_fee = (
            executed_exit
            * exit_size
            * fee_rate
        )
        equity += gross_pnl - exit_fee

        position["remaining_size"] -= exit_size
        position["realized_gross_pnl"] += gross_pnl
        position["realized_exit_fees"] += exit_fee
        position["realized_exit_slippage_cost"] += (
            abs(executed_exit - setup.take_profit_1) * exit_size
        )
        position["realized_exit_value"] += (
            executed_exit * exit_size
        )
        position["tp1_hit"] = True

        if setup.move_stop_to_break_even_after_tp1:
            stop = economic_break_even(position["entry_price"], setup.signal, fee_rate,
                                       slippage_bps, setup.break_even_mode)
            # Do not activate a stop beyond the price that caused activation.
            if ((setup.signal == Signal.LONG and stop < setup.take_profit_1)
                    or (setup.signal == Signal.SHORT and stop > setup.take_profit_1)):
                position["effective_stop"] = stop

    def update_excursion(price):
        if position is None:
            return
        distance = abs(position["entry_price"] - position["setup"].stop_loss)
        direction = 1 if position["setup"].signal == Signal.LONG else -1
        excursion = direction * (price - position["entry_price"]) / distance if distance else 0
        position["mfe_r"] = max(position["mfe_r"], excursion)
        position["mae_r"] = min(position["mae_r"], excursion)

    def fill_entry(bar, raw_entry):
        nonlocal position, pending, equity
        setup = pending["setup"]
        direction = 1 if setup.signal == Signal.LONG else -1
        executed_entry = _entry_with_slippage(raw_entry, setup.signal, slippage_bps)
        if (direction * (executed_entry - setup.stop_loss) <= 0
                or direction * (setup.take_profit_1 - executed_entry) <= 0):
            pending = None
            return
        size = setup.position_size
        if cost_aware_sizing:
            stop_fill = _exit_with_slippage(setup.stop_loss, setup.signal, slippage_bps)
            loss_per_unit = direction * (executed_entry - stop_fill) + fee_rate * (executed_entry + stop_fill)
            size = min(size, setup.target_risk_amount / loss_per_unit,
                       equity / (executed_entry * (1 + fee_rate)))
        if size <= 0:
            pending = None
            return
        entry_fee = executed_entry * size * fee_rate
        equity -= entry_fee
        position = {
            "setup": setup, "entry_price": executed_entry,
            "initial_size": size, "remaining_size": size,
            "initial_risk_amount": abs(executed_entry - setup.stop_loss) * size,
            "signal_time": pending["signal_time"], "entry_time": bar["timestamp"],
            "entry_fee": entry_fee, "entry_slippage_cost": abs(executed_entry - raw_entry) * size,
            "realized_exit_slippage_cost": 0.0, "score": pending["assessment"].score,
            "strategy": pending["assessment"].strategy, "regime": pending["structure"].regime.value,
            "tp1_hit": False, "effective_stop": setup.stop_loss,
            "realized_gross_pnl": 0.0, "realized_exit_fees": 0.0,
            "realized_exit_value": 0.0, "ambiguous_bars": 0, "mfe_r": 0.0, "mae_r": 0.0,
        }
        pending = None

    def simulate_path(bar, points):
        def get_position():
            if position is None:
                return None
            setup = position["setup"]
            return SimpleNamespace(signal=setup.signal, effective_stop=position["effective_stop"],
                                   take_profit_1=setup.take_profit_1, take_profit_2=setup.take_profit_2,
                                   tp1_hit=position["tp1_hit"])
        def cancel():
            nonlocal pending
            pending = None
        replay_price_path(points, get_position=get_position,
                          get_pending=lambda: pending["setup"] if pending is not None else None,
                          fill_entry=lambda price: fill_entry(bar, price), cancel_pending=cancel,
                          take_tp1=take_tp1, close_position=lambda price, reason: close_position(bar, price, reason),
                          observe=update_excursion)

    def process_execution_bar(bar):
        nonlocal position, pending, equity
        if position is None and pending is None:
            return
        original_position, original_pending, original_equity = deepcopy(position), deepcopy(pending), equity
        trade_count = len(trades)
        candidates = []
        path_position = original_position["setup"] if original_position else None
        for points in ohlc_paths(bar, original_pending["setup"] if original_pending else None, path_position):
            position, pending, equity = deepcopy(original_position), deepcopy(original_pending), original_equity
            del trades[trade_count:]
            simulate_path(bar, points)
            mark = equity + (_gross_pnl(position["setup"].signal, position["entry_price"],
                                      float(bar["close"]), position["remaining_size"]) if position else 0)
            new_trades = deepcopy(trades[trade_count:])
            fingerprint = (round(equity, 10), None if position is None else
                           (position["remaining_size"], position["effective_stop"], position["tp1_hit"]),
                           tuple((x.exit_reason, round(x.pnl, 10)) for x in new_trades), pending is not None)
            candidates.append((mark, deepcopy(position), deepcopy(pending), equity, new_trades, fingerprint))
        ambiguous = len({candidate[-1] for candidate in candidates}) > 1
        chosen = (min if ambiguity_policy == "conservative" else max)(candidates, key=lambda x: x[0])
        _, position, pending, equity, new_trades, _ = chosen
        del trades[trade_count:]
        if ambiguous:
            execution_diagnostics["ambiguous_bars"] += 1
            if position is not None:
                position["ambiguous_bars"] += 1
            for trade in new_trades:
                trade.ambiguous_bars += 1
        trades.extend(new_trades)
        if original_pending is not None and pending is None and position is None and not new_trades:
            execution_diagnostics["pending_cancelled"] += 1

    for i in range(start_index, len(prepared)):
        candle = prepared.iloc[i]
        if execution is None:
            execution_bars = [candle]
        else:
            start = pd.Timestamp(candle["timestamp"])
            end = start + primary_delta
            lo = int(execution_times.searchsorted(start, side="left"))
            hi = int(execution_times.searchsorted(end, side="left"))
            execution_bars = list(
                execution.iloc[lo:hi].to_dict("records")
            )
            expected = timeframe_to_milliseconds(primary_timeframe) // timeframe_to_milliseconds(execution_timeframe)
            interval = pd.to_timedelta(
                timeframe_to_milliseconds(execution_timeframe), unit="ms"
            )
            if (
                len(execution_bars) != expected
                or any(
                    pd.Timestamp(bar["timestamp"]) != start + offset * interval
                    for offset, bar in enumerate(execution_bars)
                )
            ):
                raise ValueError(
                    f"Missing or misaligned {execution_timeframe} execution candles "
                    f"for primary bar {start}."
                )
        if execution is not None:
            aggregate = {"open": float(execution_bars[0]["open"]),
                         "high": max(float(bar["high"]) for bar in execution_bars),
                         "low": min(float(bar["low"]) for bar in execution_bars),
                         "close": float(execution_bars[-1]["close"])}
            if any(not math.isclose(value, float(candle[key]), rel_tol=1e-7, abs_tol=1e-10)
                   for key, value in aggregate.items()):
                raise ValueError(f"Execution OHLC does not aggregate to primary candle {candle['timestamp']}.")
        pending_at_open = pending is not None
        for bar in execution_bars:
            process_execution_bar(bar)
            mark = equity + (_gross_pnl(position["setup"].signal, position["entry_price"],
                                      float(bar["close"]), position["remaining_size"]) if position else 0)
            peak_equity = max(peak_equity, mark)
            max_drawdown = max(max_drawdown, (peak_equity - mark) / peak_equity if peak_equity else 0)
            delta = pd.to_timedelta(timeframe_to_milliseconds(execution_timeframe or primary_timeframe), unit="ms")
            equity_curve.append({"timestamp": pd.Timestamp(bar["timestamp"]) + delta, "equity": mark})

        # Pending setups expire after full primary candles, not individual child candles.
        if pending_at_open and pending is not None:
            pending["bars_left"] -= 1
            if pending["bars_left"] <= 0:
                execution_diagnostics["pending_expired"] += 1
                pending = None

        # Mark-to-market only the still-open quantity.
        mark_equity = equity
        if position is not None:
            setup = position["setup"]
            mark_equity += _gross_pnl(
                setup.signal,
                position["entry_price"],
                float(candle["close"]),
                position["remaining_size"],
            )

        peak_equity = max(peak_equity, mark_equity)
        if peak_equity > 0:
            max_drawdown = max(
                max_drawdown,
                (peak_equity - mark_equity) / peak_equity,
            )

        # Observe rearming while a position is open as well as while flat.
        if i < len(prepared) - 1:
            decision = evaluate_at(i)
            if decision.assessment.status != SetupStatus.CONFIRMED:
                active_thesis = None
            if pending is not None:
                setup = pending["setup"]
                if setup.event_id:
                    direction = 1 if setup.signal == Signal.LONG else -1
                    cancel = setup.cancel_price if setup.cancel_price is not None else setup.stop_loss
                    context = historical_gates.apply_assessment(pending["assessment"], candle["timestamp"], context_only=True)
                    regime = decision.structure.regime.value
                    regime_ok = (regime == "RANGE" if setup.setup_type == "range_reversal" else
                                 regime == ("BULL_TREND" if direction == 1 else "BEAR_TREND"))
                    valid = direction * (float(candle["close"]) - cancel) > 0 and regime_ok and context.status == SetupStatus.CONFIRMED
                else:
                    valid = _same_pending_thesis(pending, decision)
                if not valid:
                    execution_diagnostics["pending_cancelled"] += 1
                    pending = None
            thesis_key = (decision.assessment.strategy, decision.assessment.signal)
            event_id = getattr(decision.assessment, "event_id", None)
            new_event = event_id not in seen_events if event_id else (not effective_dedupe or thesis_key != active_thesis)
            if (position is None and pending is None and new_event
                    and decision.assessment.status == SetupStatus.CONFIRMED
                    and decision.setup is not None
                    and (allow_short or decision.assessment.signal != Signal.SHORT)):
                active_thesis = thesis_key
                if event_id:
                    seen_events.add(event_id)
                execution_diagnostics["pending_created"] += 1
                pending = {"setup": decision.setup, "assessment": decision.assessment,
                           "structure": decision.structure, "signal_time": candle["timestamp"],
                           "bars_left": entry_valid_bars}

    if position is not None:
        candle = prepared.iloc[-1]
        close_position(
            candle,
            float(candle["close"]),
            (
                "END_OF_DATA_AFTER_TP1"
                if position["tp1_hit"]
                else "END_OF_DATA"
            ),
        )

    # Include liquidation fees/slippage at the end-of-data valuation.
    if equity_curve:
        equity_curve[-1]["equity"] = equity
    peak = initial_equity
    for point in equity_curve:
        peak = max(peak, point["equity"])
        max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak if peak else 0)
    trade_df = pd.DataFrame(
        [asdict(trade) for trade in trades]
    )
    (
        total_trades,
        win_rate,
        profit_factor,
        average_pnl,
    ) = calculate_trade_metrics(trade_df)

    return BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=calculate_return_pct(
            initial_equity,
            equity,
        ),
        max_drawdown_pct=max_drawdown * 100,
        total_trades=total_trades,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        average_pnl=average_pnl,
        trades=trade_df,
        execution_diagnostics=execution_diagnostics, equity_curve=equity_curve,
    )
