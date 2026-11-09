from dataclasses import asdict
import math

import pandas as pd

from trading_bot.analysis import add_indicators
from trading_bot.backtesting.metrics import (
    calculate_return_pct,
    calculate_trade_metrics,
)
from trading_bot.backtesting.models import BacktestResult, CompletedTrade
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.decision import DecisionEngine


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
) -> BacktestResult:
    """Backtest the same scored decision path used by MarketScanner.

    A confirmed setup becomes a resting reference-price entry for up to
    entry_valid_bars. Full size exits at TP2; TP1 remains informational.
    If stop and target are touched in the same OHLC candle, stop wins.
    """
    for label, value in (
        ("initial_equity", initial_equity), ("risk_percent", risk_percent),
        ("fee_rate", fee_rate), ("slippage_bps", slippage_bps),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite.")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive.")
    if not 0 < risk_percent <= 1:
        raise ValueError("risk_percent must be between 0 and 1.")
    if not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10_000:
        raise ValueError("Invalid fee rate or slippage.")
    if not 0 <= min_confirmed_score <= 100:
        raise ValueError("min_confirmed_score must be between 0 and 100.")
    if data.empty or data["timestamp"].duplicated().any():
        raise ValueError("Backtest requires nonempty, unique candle timestamps.")
    if not pd.to_datetime(data["timestamp"], utc=True).is_monotonic_increasing:
        raise ValueError("Candles must be in chronological order.")
    if entry_valid_bars <= 0:
        raise ValueError("entry_valid_bars must be positive.")
    if decision_lookback < 21:
        raise ValueError("decision_lookback must be at least 21 candles.")

    prepared = add_indicators(data.copy())
    equity = initial_equity
    peak_equity = equity
    max_drawdown = 0.0

    trades: list[CompletedTrade] = []
    pending = None
    position = None

    start_index = max(warmup_candles, 1)

    for i in range(start_index, len(prepared)):
        candle = prepared.iloc[i]
        newly_filled = False

        if position is None and pending is not None:
            reference = pending["setup"].reference_entry
            touched = (
                float(candle["low"]) <= reference <= float(candle["high"])
            )

            if touched:
                signal = pending["setup"].signal
                executed_entry = _entry_with_slippage(
                    reference,
                    signal,
                    slippage_bps,
                )

                setup = pending["setup"]
                valid_stop = (
                    0 < setup.stop_loss < executed_entry
                    if signal == Signal.LONG
                    else executed_entry < setup.stop_loss
                )
                # Make the realized modeled 1R risk include both-side fees
                # and adverse slippage at the intended stop.
                stop_execution = _exit_with_slippage(
                    setup.stop_loss, signal, slippage_bps
                )
                loss_per_unit = (
                    abs(executed_entry - stop_execution)
                    + fee_rate * (executed_entry + stop_execution)
                )
                position_size = min(
                    setup.position_size,
                    equity * risk_percent / loss_per_unit,
                ) if valid_stop and loss_per_unit > 0 else 0
                entry_fee = executed_entry * position_size * fee_rate
                risk_amount = loss_per_unit * position_size
                if position_size <= 0 or not math.isfinite(position_size):
                    pending = None
                    continue
                equity -= entry_fee
                newly_filled = True
                position = {
                    "setup": setup,
                    "qty": position_size,
                    "entry_price": executed_entry,
                    "risk_amount": risk_amount,
                    "signal_time": pending["signal_time"],
                    "entry_time": candle["timestamp"],
                    "entry_fee": entry_fee,
                    "score": pending["assessment"].score,
                    "regime": pending["structure"].regime.value,
                }
                pending = None
            else:
                pending["bars_left"] -= 1
                if pending["bars_left"] <= 0:
                    pending = None

        if position is not None:
            setup = position["setup"]
            hit_stop = False
            hit_target = False

            if setup.signal == Signal.LONG:
                hit_stop = float(candle["low"]) <= setup.stop_loss
                hit_target = float(candle["high"]) >= setup.take_profit_2
            else:
                hit_stop = float(candle["high"]) >= setup.stop_loss
                hit_target = float(candle["low"]) <= setup.take_profit_2

            exit_price = None
            exit_reason = None
            if hit_stop:
                # If an existing position gaps beyond its stop, it exits
                # at the worse open, not an impossible historical stop price.
                # Do not apply the open to a position filled later this bar.
                bar_open = float(candle["open"])
                if newly_filled:
                    exit_price = setup.stop_loss
                elif setup.signal == Signal.LONG:
                    exit_price = min(bar_open, setup.stop_loss)
                else:
                    exit_price = max(bar_open, setup.stop_loss)
                exit_reason = "STOP_LOSS"
            elif hit_target:
                exit_price = setup.take_profit_2
                exit_reason = "TAKE_PROFIT_2"

            if exit_price is not None:
                executed_exit = _exit_with_slippage(
                    exit_price,
                    setup.signal,
                    slippage_bps,
                )
                gross_pnl = _gross_pnl(
                    setup.signal,
                    position["entry_price"],
                    executed_exit,
                    position["qty"],
                )
                exit_fee = (
                    executed_exit
                    * position["qty"]
                    * fee_rate
                )
                equity += gross_pnl - exit_fee
                net_pnl = (
                    gross_pnl
                    - position["entry_fee"]
                    - exit_fee
                )
                risk_amount = position["risk_amount"]
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
                        exit_price=executed_exit,
                        stop_loss=setup.stop_loss,
                        take_profit=setup.take_profit_2,
                        position_size=position["qty"],
                        pnl=net_pnl,
                        exit_reason=exit_reason,
                        strategy="trend_continuation",
                        score=position["score"],
                        regime=position["regime"],
                        risk_amount=risk_amount,
                        r_multiple=r_multiple,
                    )
                )
                position = None

        mark_equity = equity
        if position is not None:
            setup = position["setup"]
            mark_equity += _gross_pnl(
                setup.signal,
                position["entry_price"],
                float(candle["close"]),
                position["qty"],
            )
            mark_equity -= float(candle["close"]) * position["qty"] * fee_rate

        peak_equity = max(peak_equity, mark_equity)
        if peak_equity > 0:
            max_drawdown = max(
                max_drawdown,
                (peak_equity - mark_equity) / peak_equity,
            )

        if (
            position is None
            and pending is None
            and i < len(prepared) - 1
        ):
            history = prepared.iloc[
                max(0, i - decision_lookback + 1): i + 1
            ]
            decision = DecisionEngine(
                account_equity=equity,
                risk_percent=risk_percent,
                min_confirmed_score=min_confirmed_score,
            ).evaluate(history)

            if (
                decision.assessment.status == SetupStatus.CONFIRMED
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
                    "atr": float(candle["atr_14"]),
                    "bars_left": entry_valid_bars,
                }

    if position is not None:
        candle = prepared.iloc[-1]
        setup = position["setup"]
        executed_exit = _exit_with_slippage(
            float(candle["close"]),
            setup.signal,
            slippage_bps,
        )
        gross_pnl = _gross_pnl(
            setup.signal,
            position["entry_price"],
            executed_exit,
            position["qty"],
        )
        exit_fee = executed_exit * position["qty"] * fee_rate
        equity += gross_pnl - exit_fee
        net_pnl = gross_pnl - position["entry_fee"] - exit_fee
        risk_amount = position["risk_amount"]
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
                exit_price=executed_exit,
                stop_loss=setup.stop_loss,
                take_profit=setup.take_profit_2,
                position_size=position["qty"],
                pnl=net_pnl,
                exit_reason="END_OF_DATA",
                strategy="trend_continuation",
                score=position["score"],
                regime=position["regime"],
                risk_amount=risk_amount,
                r_multiple=r_multiple,
            )
        )

    trade_df = pd.DataFrame([asdict(trade) for trade in trades])
    total_trades, win_rate, profit_factor, average_pnl = (
        calculate_trade_metrics(trade_df)
    )

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
