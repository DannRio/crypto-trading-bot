"""Candle-by-candle simulation of the live scanner's trend-continuation rules.

Signals use completed historical candles only. A qualifying signal at candle
close may fill at the next candle open, *only* inside its planned entry zone.
OHLC data cannot establish intrabar ordering; stops win simultaneous touches.
This is a research simulator, not an exchange matching engine.
"""
from dataclasses import asdict
import math

import pandas as pd

from trading_bot.analysis import analyze_market_structure
from trading_bot.backtesting.metrics import calculate_return_pct, calculate_trade_metrics
from trading_bot.backtesting.models import BacktestResult, CompletedTrade
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.planning import create_trade_setup
from trading_bot.strategies import evaluate_trend_continuation


def _entry_with_slippage(price: float, signal: Signal, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000
    return price * (1 + slip if signal == Signal.LONG else 1 - slip)


def _exit_with_slippage(price: float, signal: Signal, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000
    return price * (1 - slip if signal == Signal.LONG else 1 + slip)


def _gross_pnl(signal: Signal, entry: float, exit: float, qty: float) -> float:
    return (exit - entry) * qty * (1 if signal == Signal.LONG else -1)


def _validate_input(
    data: pd.DataFrame,
    initial_equity: float,
    risk_percent: float,
    max_notional_percent: float,
    min_confirmed_score: int,
    fee_rate: float,
    slippage_bps: float,
) -> None:
    required = {"timestamp", "open", "high", "low", "close", "volume",
                "ema_50", "ema_200", "rsi_14", "atr_14"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Missing backtest columns: {sorted(missing)}")
    if len(data) < 220:
        raise ValueError("At least 220 complete candles are required.")
    if data["timestamp"].isna().any() or not pd.to_datetime(data["timestamp"], utc=True).is_monotonic_increasing:
        raise ValueError("Candles must be timestamped in chronological order.")
    if data["timestamp"].duplicated().any():
        raise ValueError("Duplicate candle timestamps are not allowed.")
    for label, value in [
        ("initial_equity", initial_equity), ("risk_percent", risk_percent),
        ("max_notional_percent", max_notional_percent),
        ("fee_rate", fee_rate), ("slippage_bps", slippage_bps),
    ]:
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite.")
    if initial_equity <= 0 or not 0 < risk_percent <= 1 or max_notional_percent <= 0:
        raise ValueError("Equity, risk percent and notional cap must be positive and valid.")
    if not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10_000:
        raise ValueError("Fee rate or slippage is outside supported bounds.")
    if not 0 <= min_confirmed_score <= 100:
        raise ValueError("Minimum score must be between 0 and 100.")
    prices = data[["open", "high", "low", "close"]]
    if not prices.notna().all().all() or not (prices > 0).all().all():
        raise ValueError("OHLC prices must be finite and positive.")
    if not ((data["high"] >= data[["open", "close", "low"]].max(axis=1)) &
            (data["low"] <= data[["open", "close", "high"]].min(axis=1))).all():
        raise ValueError("Inconsistent OHLC bars.")


def run_backtest(
    data: pd.DataFrame,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    min_confirmed_score: int = 70,
    max_notional_percent: float = 1.0,
    allow_short: bool = True,
) -> BacktestResult:
    """Backtest the same *qualification and planning rules* as MarketScanner.

    The scanner references the latest close. Execution is simulated at the
    following bar's open within the original entry zone; stop and TP2 stay at
    the precommitted absolute levels. Size is recalculated at the actual fill
    to include entry/exit fees and worst-case specified stop slippage.

    No financing, funding, liquidation, exchange contract multiplier,
    partial fills, portfolio exposure, or margin model is simulated.
    """
    _validate_input(data, initial_equity, risk_percent,
                    max_notional_percent, min_confirmed_score,
                    fee_rate, slippage_bps)
    equity = initial_equity
    peak = initial_equity
    drawdown = 0.0
    trades: list[CompletedTrade] = []
    position = None
    pending = None
    signals_seen = 0
    entries_skipped = 0

    def finish_trade(bar, exit_level: float, reason: str) -> None:
        nonlocal equity, position
        signal = position["signal"]
        actual_exit = _exit_with_slippage(exit_level, signal, slippage_bps)
        qty = position["qty"]
        gross = _gross_pnl(signal, position["entry"], actual_exit, qty)
        exit_fee = qty * actual_exit * fee_rate
        equity += gross - exit_fee
        trades.append(CompletedTrade(
            signal=signal.value,
            signal_time=position["signal_time"],
            entry_time=position["entry_time"],
            exit_time=bar["timestamp"],
            entry_price=position["entry"],
            exit_price=actual_exit,
            stop_loss=position["stop"],
            take_profit=position["target"],
            position_size=qty,
            pnl=gross - position["entry_fee"] - exit_fee,
            exit_reason=reason,
        ))
        position = None

    for i in range(len(data)):
        bar = data.iloc[i]
        opening = float(bar["open"])

        # Signal is from the *previous* completed candle; never act on
        # current bar's close/indicators to create a current-bar entry.
        if pending is not None and position is None:
            plan = pending["plan"]
            signal = plan.signal
            entry = _entry_with_slippage(opening, signal, slippage_bps)

            valid_zone = plan.entry_low <= entry <= plan.entry_high
            valid_levels = (0 < plan.stop_loss < entry < plan.take_profit_2
                            if signal == Signal.LONG else
                            0 < plan.take_profit_2 < entry < plan.stop_loss)
            if valid_zone and valid_levels and equity > 0:
                stop_execution = _exit_with_slippage(plan.stop_loss, signal, slippage_bps)
                # Include both fees and modeled stop slippage in the cash risk
                # denominator (without assuming the stop always fills there).
                cash_loss_per_unit = (abs(entry - stop_execution)
                                      + fee_rate * (entry + stop_execution))
                qty = min(
                    equity * risk_percent / cash_loss_per_unit,
                    equity * max_notional_percent / entry,
                )
                if qty > 0 and math.isfinite(qty):
                    entry_fee = entry * qty * fee_rate
                    equity -= entry_fee
                    position = {
                        "signal": signal,
                        "signal_time": pending["time"],
                        "entry_time": bar["timestamp"],
                        "entry": entry,
                        "qty": qty,
                        "entry_fee": entry_fee,
                        "stop": plan.stop_loss,
                        "target": plan.take_profit_2,
                    }
                else:
                    entries_skipped += 1
            else:
                entries_skipped += 1
            pending = None

        if position is not None:
            signal = position["signal"]
            stop = position["stop"]
            target = position["target"]
            high, low = float(bar["high"]), float(bar["low"])

            stop_hit = low <= stop if signal == Signal.LONG else high >= stop
            target_hit = high >= target if signal == Signal.LONG else low <= target

            if stop_hit:
                # A bar opening past the stop must exit at the worse open,
                # not the unreachable stop level; apply adverse slippage too.
                exit_level = min(opening, stop) if signal == Signal.LONG else max(opening, stop)
                finish_trade(bar, exit_level, "STOP_LOSS")
            elif target_hit:
                finish_trade(bar, target, "TAKE_PROFIT")

        mark_equity = equity
        if position is not None:
            unrealized = _gross_pnl(
                position["signal"], position["entry"], float(bar["close"]),
                position["qty"],
            )
            # Reserve estimated closing fee to avoid overstating marked equity.
            mark_equity += unrealized - float(bar["close"]) * position["qty"] * fee_rate

        peak = max(peak, mark_equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - mark_equity) / peak)

        # Use only history up to this candle close. Scanner's 220-bar warmup
        # excludes early candles from being called CONFIRMED.
        if position is None and i >= 219 and i < len(data) - 1 and equity > 0:
            history = data.iloc[: i + 1]
            structure = analyze_market_structure(history)
            assessment = evaluate_trend_continuation(
                history, structure, min_confirmed_score=min_confirmed_score
            )
            if assessment.status == SetupStatus.CONFIRMED:
                if not allow_short and assessment.signal == Signal.SHORT:
                    continue
                candle_close = float(bar["close"])
                atr = float(bar["atr_14"])
                plan = create_trade_setup(
                    signal=assessment.signal,
                    status=assessment.status,
                    score=assessment.score,
                    reasons=assessment.reasons,
                    entry_price=candle_close,
                    atr=atr,
                    structure=structure,
                    account_equity=equity,
                    risk_percent=risk_percent,
                    max_notional_percent=max_notional_percent,
                )
                if plan is not None:
                    pending = {"time": bar["timestamp"], "plan": plan}
                    signals_seen += 1

    # Close any open position at the final *completed* candle close.
    if position is not None:
        finish_trade(data.iloc[-1], float(data.iloc[-1]["close"]), "END_OF_DATA")
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)

    trade_df = pd.DataFrame([asdict(trade) for trade in trades])
    total, win_rate, factor, average = calculate_trade_metrics(trade_df)
    return BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=calculate_return_pct(initial_equity, equity),
        max_drawdown_pct=drawdown * 100,
        total_trades=total,
        win_rate_pct=win_rate,
        profit_factor=factor,
        average_pnl=average,
        trades=trade_df,
        signals_seen=signals_seen,
        entries_skipped=entries_skipped,
    )
