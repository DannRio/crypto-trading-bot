from dataclasses import asdict

import pandas as pd

from trading_bot.backtesting.metrics import (
    calculate_return_pct,
    calculate_trade_metrics,
)
from trading_bot.backtesting.models import (
    BacktestResult,
    CompletedTrade,
)
from trading_bot.core.enums import Signal
from trading_bot.risk.risk_manager import create_trade_plan
from trading_bot.strategies.trend import generate_signal


def _entry_with_slippage(
    price: float,
    signal: Signal,
    slippage_bps: float,
) -> float:
    slip = slippage_bps / 10_000

    if signal == Signal.LONG:
        return price * (1 + slip)

    return price * (1 - slip)


def _exit_with_slippage(
    price: float,
    signal: Signal,
    slippage_bps: float,
) -> float:
    slip = slippage_bps / 10_000

    if signal == Signal.LONG:
        return price * (1 - slip)

    return price * (1 + slip)


def _gross_pnl(
    signal: Signal,
    entry_price: float,
    exit_price: float,
    position_size: float,
) -> float:
    if signal == Signal.LONG:
        return (
            exit_price - entry_price
        ) * position_size

    return (
        entry_price - exit_price
    ) * position_size


def run_backtest(
    data: pd.DataFrame,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    atr_stop_multiplier: float = 2.0,
    reward_to_risk: float = 2.0,
    fee_rate: float = 0.0,
    slippage_bps: float = 0.0,
) -> BacktestResult:

    equity = initial_equity

    peak_equity = equity
    max_drawdown = 0.0

    trades = []

    position = None
    pending_signal = None

    for i in range(len(data)):
        candle = data.iloc[i]

        # ---------------------------------------------
        # 1. Enter using a signal generated from the
        #    previous completed candle.
        # ---------------------------------------------
        if position is None and pending_signal is not None:
            signal = pending_signal["signal"]

            entry_price = _entry_with_slippage(
                float(candle["open"]),
                signal,
                slippage_bps,
            )

            plan = create_trade_plan(
                signal=signal,
                entry_price=entry_price,
                atr=pending_signal["atr"],
                account_equity=equity,
                risk_percent=risk_percent,
                atr_stop_multiplier=atr_stop_multiplier,
                reward_to_risk=reward_to_risk,
            )

            if plan is not None:
                entry_fee = (
                    plan.entry_price
                    * plan.position_size
                    * fee_rate
                )

                equity -= entry_fee

                position = {
                    "plan": plan,
                    "signal_time": pending_signal["time"],
                    "entry_time": candle["timestamp"],
                    "entry_fee": entry_fee,
                }

            pending_signal = None

        # ---------------------------------------------
        # 2. Check stop-loss and take-profit.
        # ---------------------------------------------
        if position is not None:
            plan = position["plan"]

            hit_stop = False
            hit_target = False

            if plan.signal == Signal.LONG:
                hit_stop = candle["low"] <= plan.stop_loss
                hit_target = candle["high"] >= plan.take_profit

            elif plan.signal == Signal.SHORT:
                hit_stop = candle["high"] >= plan.stop_loss
                hit_target = candle["low"] <= plan.take_profit

            exit_price = None
            exit_reason = None

            # If both stop and target occur within the
            # same OHLC candle, we cannot know which
            # happened first. Use the conservative result.
            if hit_stop:
                exit_price = plan.stop_loss
                exit_reason = "STOP_LOSS"

            elif hit_target:
                exit_price = plan.take_profit
                exit_reason = "TAKE_PROFIT"

            if exit_price is not None:
                executed_exit = _exit_with_slippage(
                    exit_price,
                    plan.signal,
                    slippage_bps,
                )

                gross_pnl = _gross_pnl(
                    signal=plan.signal,
                    entry_price=plan.entry_price,
                    exit_price=executed_exit,
                    position_size=plan.position_size,
                )

                exit_fee = (
                    executed_exit
                    * plan.position_size
                    * fee_rate
                )

                equity += gross_pnl - exit_fee

                net_pnl = (
                    gross_pnl
                    - position["entry_fee"]
                    - exit_fee
                )

                trades.append(
                    CompletedTrade(
                        signal=plan.signal.value,
                        signal_time=position["signal_time"],
                        entry_time=position["entry_time"],
                        exit_time=candle["timestamp"],
                        entry_price=plan.entry_price,
                        exit_price=executed_exit,
                        stop_loss=plan.stop_loss,
                        take_profit=plan.take_profit,
                        position_size=plan.position_size,
                        pnl=net_pnl,
                        exit_reason=exit_reason,
                    )
                )

                position = None

        # ---------------------------------------------
        # 3. Calculate mark-to-market drawdown.
        # ---------------------------------------------
        mark_equity = equity

        if position is not None:
            plan = position["plan"]

            unrealized = _gross_pnl(
                signal=plan.signal,
                entry_price=plan.entry_price,
                exit_price=float(candle["close"]),
                position_size=plan.position_size,
            )

            mark_equity += unrealized

        peak_equity = max(
            peak_equity,
            mark_equity,
        )

        if peak_equity > 0:
            drawdown = (
                peak_equity - mark_equity
            ) / peak_equity

            max_drawdown = max(
                max_drawdown,
                drawdown,
            )

        # ---------------------------------------------
        # 4. Generate signal only after candle closes.
        #    Any entry occurs on the NEXT candle.
        # ---------------------------------------------
        if position is None and i < len(data) - 1:
            history = data.iloc[: i + 1]

            result = generate_signal(history)

            if result.signal != Signal.NO_TRADE:
                atr = float(candle["atr_14"])

                if pd.notna(atr) and atr > 0:
                    pending_signal = {
                        "signal": result.signal,
                        "time": candle["timestamp"],
                        "atr": atr,
                    }

    # ---------------------------------------------
    # 5. Close remaining position at end of dataset.
    # ---------------------------------------------
    if position is not None:
        candle = data.iloc[-1]
        plan = position["plan"]

        executed_exit = _exit_with_slippage(
            float(candle["close"]),
            plan.signal,
            slippage_bps,
        )

        gross_pnl = _gross_pnl(
            signal=plan.signal,
            entry_price=plan.entry_price,
            exit_price=executed_exit,
            position_size=plan.position_size,
        )

        exit_fee = (
            executed_exit
            * plan.position_size
            * fee_rate
        )

        equity += gross_pnl - exit_fee

        net_pnl = (
            gross_pnl
            - position["entry_fee"]
            - exit_fee
        )

        trades.append(
            CompletedTrade(
                signal=plan.signal.value,
                signal_time=position["signal_time"],
                entry_time=position["entry_time"],
                exit_time=candle["timestamp"],
                entry_price=plan.entry_price,
                exit_price=executed_exit,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                position_size=plan.position_size,
                pnl=net_pnl,
                exit_reason="END_OF_DATA",
            )
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

    total_return_pct = calculate_return_pct(
        initial_equity=initial_equity,
        final_equity=equity,
    )

    return BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_drawdown * 100,
        total_trades=total_trades,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        average_pnl=average_pnl,
        trades=trade_df,
    )