from dataclasses import asdict

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
    primary_timeframe: str = "1h",
    confirmation_timeframes: tuple[str, ...] = (),
    entry_timeframe: str | None = None,
    secondary_data: dict[str, pd.DataFrame] | None = None,
    execution_timeframe: str | None = None,
) -> BacktestResult:
    """Backtest the same scored decision path used by MarketScanner.

    Confirmed setups create resting entry zones for up to entry_valid_bars.
    Pending setups are revalidated at each candle close before they can carry
    into another candle. TP1 can realize a configured partial position and,
    optionally, move the remaining stop to break-even. Same-candle ambiguity
    remains conservative: an active stop wins over a target.
    """
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
        ).evaluate(history)
        return historical_gates.apply(decision, prepared.iloc[index]["timestamp"])

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
        position["realized_exit_value"] += (
            executed_exit * exit_size
        )
        position["tp1_hit"] = True

        if setup.move_stop_to_break_even_after_tp1:
            position["effective_stop"] = position["entry_price"]

    def process_execution_bar(bar):
        nonlocal position, pending, equity
        if position is None and pending is not None:
            raw_entry = _entry_price_from_zone(
                bar,
                pending["setup"],
            )

            if raw_entry is not None:
                setup = pending["setup"]
                executed_entry = _entry_with_slippage(
                    raw_entry,
                    setup.signal,
                    slippage_bps,
                )
                entry_fee = (
                    executed_entry
                    * setup.position_size
                    * fee_rate
                )
                initial_risk_amount = (
                    abs(executed_entry - setup.stop_loss)
                    * setup.position_size
                )
                equity -= entry_fee
                position = {
                    "setup": setup,
                    "entry_price": executed_entry,
                    "initial_size": setup.position_size,
                    "remaining_size": setup.position_size,
                    "initial_risk_amount": initial_risk_amount,
                    "signal_time": pending["signal_time"],
                    "entry_time": bar["timestamp"],
                    "entry_fee": entry_fee,
                    "score": pending["assessment"].score,
                    "strategy": pending["assessment"].strategy,
                    "regime": pending["structure"].regime.value,
                    "tp1_hit": False,
                    "effective_stop": setup.stop_loss,
                    "realized_gross_pnl": 0.0,
                    "realized_exit_fees": 0.0,
                    "realized_exit_value": 0.0,
                }
                pending = None

        # Manage the open position. An active stop always wins when both stop
        # and target are reachable inside the same OHLC bar.
        if position is not None:
            setup = position["setup"]
            effective_stop = position["effective_stop"]

            if setup.signal == Signal.LONG:
                hit_stop = (
                    float(bar["low"]) <= effective_stop
                )
                hit_tp1 = (
                    not position["tp1_hit"]
                    and setup.take_profit_1_fraction > 0
                    and float(bar["high"]) >= setup.take_profit_1
                )
                hit_tp2 = (
                    float(bar["high"]) >= setup.take_profit_2
                )
            else:
                hit_stop = (
                    float(bar["high"]) >= effective_stop
                )
                hit_tp1 = (
                    not position["tp1_hit"]
                    and setup.take_profit_1_fraction > 0
                    and float(bar["low"]) <= setup.take_profit_1
                )
                hit_tp2 = (
                    float(bar["low"]) <= setup.take_profit_2
                )

            if hit_stop:
                close_position(
                    bar,
                    (
                        min(effective_stop, float(bar["open"]))
                        if setup.signal == Signal.LONG
                        else max(effective_stop, float(bar["open"]))
                    ),
                    (
                        "STOP_LOSS_AFTER_TP1"
                        if position["tp1_hit"]
                        else "STOP_LOSS"
                    ),
                )
            else:
                if hit_tp1:
                    take_tp1()

                if position is not None:
                    effective_stop = position["effective_stop"]
                    if setup.signal == Signal.LONG:
                        stop_after_tp1 = (
                            position["tp1_hit"]
                            and float(bar["low"])
                            <= effective_stop
                        )
                    else:
                        stop_after_tp1 = (
                            position["tp1_hit"]
                            and float(bar["high"])
                            >= effective_stop
                        )

                    # If a new break-even stop and TP2 are both inside the same
                    # bar after TP1, use the conservative outcome.
                    if stop_after_tp1:
                        close_position(
                            bar,
                            effective_stop,
                            "STOP_LOSS_AFTER_TP1",
                        )
                    elif hit_tp2:
                        close_position(
                            bar,
                            setup.take_profit_2,
                            (
                                "TAKE_PROFIT_2_AFTER_TP1"
                                if position["tp1_hit"]
                                else "TAKE_PROFIT_2"
                            ),
                        )


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
        pending_at_open = pending is not None
        for bar in execution_bars:
            process_execution_bar(bar)

        # Pending setups expire after full primary candles, not individual child candles.
        if pending_at_open and pending is not None:
            pending["bars_left"] -= 1
            if pending["bars_left"] <= 0:
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

        # Revalidate an unfilled setup only after this candle has closed. That
        # avoids using future close information to decide an intrabar fill.
        if position is None and i < len(prepared) - 1:
            decision = evaluate_at(i)

            if (
                pending is not None
                and not _same_pending_thesis(
                    pending,
                    decision,
                )
            ):
                pending = None

            if (
                pending is None
                and decision.assessment.status
                == SetupStatus.CONFIRMED
                and decision.setup is not None
                and (
                    allow_short
                    or decision.assessment.signal != Signal.SHORT
                )
            ):
                pending = {
                    "setup": decision.setup,
                    "assessment": decision.assessment,
                    "structure": decision.structure,
                    "signal_time": candle["timestamp"],
                    "bars_left": entry_valid_bars,
                }

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
    )
