from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping

import numpy as np
import pandas as pd

from .engine import RegimeSignalEngine
from .models import (
    BacktestResult,
    BacktestTrade,
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
)
from .validation import prepare_ohlcv, timeframe_to_timedelta


@dataclass
class _PendingEntry:
    index: int
    signal: MarketSignal


@dataclass
class _Position:
    side: PositionSide
    setup_type: SetupType
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    initial_stop: float
    stop: float
    target1: float
    target2: float
    target3: float
    original_quantity: float
    remaining_quantity: float
    initial_risk: float
    opening_fee: float
    accumulated_fees: float
    gross_pnl: float = 0.0
    closed_value_sum: float = 0.0
    closed_quantity: float = 0.0
    tp1_filled: bool = False
    tp2_filled: bool = False
    highest_high: float = -math.inf
    lowest_low: float = math.inf
    bars_held: int = 0
    last_exit_reason: str = ""


class RegimeBacktester:
    """Closed-candle, next-bar-entry backtester for the regime engine.

    Signals are formed only after a candle closes. Entries and regime-flip exits
    execute at the following candle open. If a stop and target are both touched
    inside one candle, the stop is assumed to fill first.
    """

    def __init__(self, engine: RegimeSignalEngine | None = None):
        self.engine = engine or RegimeSignalEngine()

    def run(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
    ) -> BacktestResult:
        if starting_equity <= 0 or not math.isfinite(starting_equity):
            raise ValueError("starting_equity must be positive and finite.")
        execution = execution_timeframe.strip().lower()
        raw_normalized = self.engine._normalize_frames(frames)
        normalized = {
            timeframe: prepare_ohlcv(frame)
            for timeframe, frame in raw_normalized.items()
        }
        self.engine._validate_timeframe_hierarchy(tuple(normalized), execution)
        if execution not in normalized:
            raise ValueError(f"Execution timeframe {execution!r} is not available.")
        required_execution_bars = self.engine.config.minimum_bars + 2
        if len(normalized[execution]) < required_execution_bars:
            raise ValueError(
                f"Backtest needs at least {required_execution_bars} execution candles."
            )
        short_contexts = {
            timeframe: len(frame)
            for timeframe, frame in normalized.items()
            if timeframe != execution and len(frame) < self.engine.config.minimum_bars
        }
        if short_contexts:
            raise ValueError(
                "Every requested context timeframe needs at least "
                f"{self.engine.config.minimum_bars} candles; received {short_contexts}."
            )

        feature_frames = self.engine.prepare_feature_frames(
            normalized,
            execution_timeframe=execution,
        )
        execution_frame = feature_frames[execution].reset_index(drop=True)
        execution_delta = timeframe_to_timedelta(execution)
        risk_pct = (
            self.engine.config.risk.risk_percent
            if risk_percent is None
            else float(risk_percent)
        )
        notional_multiple = (
            self.engine.config.risk.maximum_notional_multiple
            if maximum_notional_multiple is None
            else float(maximum_notional_multiple)
        )
        if not (0 < risk_pct <= self.engine.config.risk.maximum_risk_percent):
            raise ValueError("risk_percent is outside configured limits.")
        if notional_multiple <= 0:
            raise ValueError("maximum_notional_multiple must be positive.")

        cash_equity = float(starting_equity)
        position: _Position | None = None
        pending_entry: _PendingEntry | None = None
        pending_regime_exit = False
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple[datetime, float]] = []
        fee_rate = self.engine.config.risk.fee_bps / 10_000.0
        slippage_rate = self.engine.config.risk.slippage_bps / 10_000.0
        minimum_index = self.engine.config.minimum_bars - 1

        for index, row in execution_frame.iterrows():
            timestamp = row["timestamp"].to_pydatetime()

            if position is not None and pending_regime_exit:
                exit_price = self._slipped_price(
                    float(row["open"]),
                    position.side,
                    is_entry=False,
                    slippage_rate=slippage_rate,
                )
                cash_equity = self._close_quantity(
                    position,
                    position.remaining_quantity,
                    exit_price,
                    fee_rate,
                    cash_equity,
                    "regime_flip",
                )
                trades.append(self._finalize_trade(position, timestamp))
                position = None
                pending_regime_exit = False

            if position is None and pending_entry is not None and pending_entry.index == index:
                plan = pending_entry.signal.entry_plan
                if plan is not None:
                    position, cash_equity = self._open_position(
                        plan=plan,
                        signal=pending_entry.signal,
                        row=row,
                        equity=cash_equity,
                        risk_percent=risk_pct,
                        maximum_notional_multiple=notional_multiple,
                        fee_rate=fee_rate,
                        slippage_rate=slippage_rate,
                    )
                pending_entry = None

            if position is not None:
                position.bars_held += 1
                position.highest_high = max(position.highest_high, float(row["high"]))
                position.lowest_low = min(position.lowest_low, float(row["low"]))
                position, cash_equity, completed = self._manage_position(
                    position=position,
                    row=row,
                    timestamp=timestamp,
                    equity=cash_equity,
                    fee_rate=fee_rate,
                    slippage_rate=slippage_rate,
                )
                if completed is not None:
                    trades.append(completed)

            decision_signal: MarketSignal | None = None
            if index >= minimum_index and index < len(execution_frame) - 1:
                decision_time = row["timestamp"] + execution_delta
                decision_frames = self._frames_at_decision(
                    feature_frames,
                    execution_timeframe=execution,
                    execution_index=index,
                    decision_time=decision_time,
                )
                if decision_frames is not None:
                    decision_signal = self.engine.analyze_prepared(
                        symbol=symbol,
                        feature_frames=decision_frames,
                        execution_timeframe=execution,
                        equity=max(cash_equity, 1e-9),
                        risk_percent=risk_pct,
                        maximum_notional_multiple=notional_multiple,
                    )

            if position is not None and decision_signal is not None:
                pending_regime_exit = self._opposite_regime(
                    position.side,
                    decision_signal.regime,
                    decision_signal.score,
                )
            elif (
                position is None
                and pending_entry is None
                and decision_signal is not None
                and decision_signal.entry_plan is not None
                and decision_signal.action in {SignalAction.LONG, SignalAction.SHORT}
            ):
                pending_entry = _PendingEntry(index=index + 1, signal=decision_signal)

            marked_equity = cash_equity
            if position is not None:
                marked_equity += self._unrealized_pnl(position, float(row["close"]))
            equity_curve.append((timestamp, float(marked_equity)))

        if position is not None:
            final_row = execution_frame.iloc[-1]
            final_time = final_row["timestamp"].to_pydatetime()
            exit_price = self._slipped_price(
                float(final_row["close"]),
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            cash_equity = self._close_quantity(
                position,
                position.remaining_quantity,
                exit_price,
                fee_rate,
                cash_equity,
                "end_of_data",
            )
            trades.append(self._finalize_trade(position, final_time))
            equity_curve[-1] = (final_time, cash_equity)

        return self._build_result(
            symbol=symbol,
            execution_timeframe=execution,
            starting_equity=starting_equity,
            ending_equity=cash_equity,
            trades=trades,
            equity_curve=equity_curve,
        )

    def _frames_at_decision(
        self,
        feature_frames: Mapping[str, pd.DataFrame],
        *,
        execution_timeframe: str,
        execution_index: int,
        decision_time: pd.Timestamp,
    ) -> dict[str, pd.DataFrame] | None:
        selected: dict[str, pd.DataFrame] = {}
        for timeframe, frame in feature_frames.items():
            if timeframe == execution_timeframe:
                selected[timeframe] = frame.iloc[: execution_index + 1]
                continue
            close_times = frame["timestamp"] + timeframe_to_timedelta(timeframe)
            available = frame.loc[close_times <= decision_time]
            if len(available) < self.engine.config.minimum_bars:
                return None
            selected[timeframe] = available
        return selected

    def _open_position(
        self,
        *,
        plan: EntryPlan,
        signal: MarketSignal,
        row: pd.Series,
        equity: float,
        risk_percent: float,
        maximum_notional_multiple: float,
        fee_rate: float,
        slippage_rate: float,
    ) -> tuple[_Position | None, float]:
        side = plan.side
        raw_open = float(row["open"])
        if not (plan.entry_low <= raw_open <= plan.entry_high):
            return None, equity
        entry = self._slipped_price(
            raw_open,
            side,
            is_entry=True,
            slippage_rate=slippage_rate,
        )
        if not (plan.entry_low <= entry <= plan.entry_high):
            return None, equity
        stop = plan.stop_loss
        if side == PositionSide.LONG:
            risk_distance = entry - stop
        else:
            risk_distance = stop - entry
        if risk_distance <= 0 or not math.isfinite(risk_distance):
            return None, equity
        direction = 1.0 if side == PositionSide.LONG else -1.0
        target1 = entry + direction * plan.reward_to_risk_1 * risk_distance
        target2 = entry + direction * plan.reward_to_risk_2 * risk_distance
        target3 = entry + direction * plan.reward_to_risk_3 * risk_distance

        target_risk = equity * risk_percent / 100.0
        total_cost_rate = (
            self.engine.config.risk.fee_bps + self.engine.config.risk.slippage_bps
        ) / 10_000.0
        estimated_cost_per_unit = (entry + stop) * total_cost_rate
        total_risk_per_unit = risk_distance + estimated_cost_per_unit
        quantity_by_risk = target_risk / total_risk_per_unit
        quantity_by_notional = equity * maximum_notional_multiple / entry
        quantity = min(quantity_by_risk, quantity_by_notional)
        if quantity <= 0 or not math.isfinite(quantity):
            return None, equity
        opening_fee = entry * quantity * fee_rate
        equity_after_fee = equity - opening_fee
        if equity_after_fee <= 0:
            return None, equity

        position = _Position(
            side=side,
            setup_type=plan.setup_type,
            signal_time=signal.generated_at,
            entry_time=row["timestamp"].to_pydatetime(),
            entry_price=entry,
            initial_stop=stop,
            stop=stop,
            target1=target1,
            target2=target2,
            target3=target3,
            original_quantity=quantity,
            remaining_quantity=quantity,
            initial_risk=quantity * total_risk_per_unit,
            opening_fee=opening_fee,
            accumulated_fees=opening_fee,
            highest_high=float(row["open"]),
            lowest_low=float(row["open"]),
        )
        return position, equity_after_fee

    def _manage_position(
        self,
        *,
        position: _Position,
        row: pd.Series,
        timestamp: datetime,
        equity: float,
        fee_rate: float,
        slippage_rate: float,
    ) -> tuple[_Position | None, float, BacktestTrade | None]:
        high = float(row["high"])
        low = float(row["low"])
        long_side = position.side == PositionSide.LONG
        stop_hit = low <= position.stop if long_side else high >= position.stop

        # Conservative treatment of ambiguous intrabar paths. A gap through the
        # stop fills from the opening price rather than the unavailable stop level.
        if stop_hit:
            raw_stop_fill = position.stop
            if long_side and float(row["open"]) < position.stop:
                raw_stop_fill = float(row["open"])
            elif not long_side and float(row["open"]) > position.stop:
                raw_stop_fill = float(row["open"])
            fill = self._slipped_price(
                raw_stop_fill,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                "stop_loss" if not position.tp1_filled else "trailing_or_break_even_stop",
            )
            return None, equity, self._finalize_trade(position, timestamp)

        target1_hit = high >= position.target1 if long_side else low <= position.target1
        target2_hit = high >= position.target2 if long_side else low <= position.target2
        target3_hit = high >= position.target3 if long_side else low <= position.target3
        risk = self.engine.config.risk

        if target1_hit and not position.tp1_filled:
            quantity = min(
                position.remaining_quantity,
                position.original_quantity * risk.take_profit_1_fraction,
            )
            fill = self._slipped_price(
                position.target1,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            equity = self._close_quantity(
                position,
                quantity,
                fill,
                fee_rate,
                equity,
                "take_profit_1",
            )
            position.tp1_filled = True
            position.stop = position.entry_price

        if target2_hit and not position.tp2_filled and position.remaining_quantity > 0:
            quantity = min(
                position.remaining_quantity,
                position.original_quantity * risk.take_profit_2_fraction,
            )
            fill = self._slipped_price(
                position.target2,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            equity = self._close_quantity(
                position,
                quantity,
                fill,
                fee_rate,
                equity,
                "take_profit_2",
            )
            position.tp2_filled = True

        if target3_hit and position.remaining_quantity > 0:
            fill = self._slipped_price(
                position.target3,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                "take_profit_3",
            )
            return None, equity, self._finalize_trade(position, timestamp)

        if position.bars_held >= risk.max_holding_bars and position.remaining_quantity > 0:
            fill = self._slipped_price(
                float(row["close"]),
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                "time_stop",
            )
            return None, equity, self._finalize_trade(position, timestamp)

        # Update a trailing stop only after the candle closes; it becomes active
        # on the next candle and therefore cannot use an unknown intrabar path.
        if position.tp2_filled and position.remaining_quantity > 0:
            atr_value = float(row["atr"])
            if long_side:
                position.stop = max(
                    position.stop,
                    position.highest_high - risk.trailing_atr_multiple * atr_value,
                )
            else:
                position.stop = min(
                    position.stop,
                    position.lowest_low + risk.trailing_atr_multiple * atr_value,
                )

        return position, equity, None

    @staticmethod
    def _slipped_price(
        price: float,
        side: PositionSide,
        *,
        is_entry: bool,
        slippage_rate: float,
    ) -> float:
        if side == PositionSide.LONG:
            multiplier = 1.0 + slippage_rate if is_entry else 1.0 - slippage_rate
        else:
            multiplier = 1.0 - slippage_rate if is_entry else 1.0 + slippage_rate
        return price * multiplier

    @staticmethod
    def _close_quantity(
        position: _Position,
        quantity: float,
        exit_price: float,
        fee_rate: float,
        equity: float,
        reason: str,
    ) -> float:
        quantity = min(quantity, position.remaining_quantity)
        if quantity <= 0:
            return equity
        if position.side == PositionSide.LONG:
            gross = (exit_price - position.entry_price) * quantity
        else:
            gross = (position.entry_price - exit_price) * quantity
        closing_fee = exit_price * quantity * fee_rate
        position.gross_pnl += gross
        position.accumulated_fees += closing_fee
        position.closed_value_sum += exit_price * quantity
        position.closed_quantity += quantity
        position.remaining_quantity -= quantity
        position.last_exit_reason = reason
        return equity + gross - closing_fee

    @staticmethod
    def _unrealized_pnl(position: _Position, price: float) -> float:
        if position.side == PositionSide.LONG:
            return (price - position.entry_price) * position.remaining_quantity
        return (position.entry_price - price) * position.remaining_quantity

    def _opposite_regime(
        self,
        side: PositionSide,
        regime: MarketRegime,
        score: float,
    ) -> bool:
        thresholds = self.engine.config.signals
        if side == PositionSide.LONG:
            return (
                regime in {MarketRegime.BEARISH, MarketRegime.STRONG_BEARISH}
                or score <= thresholds.bearish_score
            )
        return (
            regime in {MarketRegime.BULLISH, MarketRegime.STRONG_BULLISH}
            or score >= thresholds.bullish_score
        )

    @staticmethod
    def _finalize_trade(position: _Position, exit_time: datetime) -> BacktestTrade:
        average_exit = (
            position.closed_value_sum / position.closed_quantity
            if position.closed_quantity > 0
            else position.entry_price
        )
        net = position.gross_pnl - position.accumulated_fees
        r_multiple = net / position.initial_risk if position.initial_risk > 0 else 0.0
        return BacktestTrade(
            side=position.side,
            setup_type=position.setup_type,
            signal_time=position.signal_time,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=average_exit,
            initial_stop=position.initial_stop,
            quantity=position.original_quantity,
            gross_pnl=position.gross_pnl,
            fees=position.accumulated_fees,
            net_pnl=net,
            initial_risk=position.initial_risk,
            r_multiple=r_multiple,
            exit_reason=position.last_exit_reason or "unknown",
            bars_held=position.bars_held,
        )

    @staticmethod
    def _build_result(
        *,
        symbol: str,
        execution_timeframe: str,
        starting_equity: float,
        ending_equity: float,
        trades: list[BacktestTrade],
        equity_curve: list[tuple[datetime, float]],
    ) -> BacktestResult:
        net_pnl = ending_equity - starting_equity
        total_return = net_pnl / starting_equity * 100.0
        net_values = np.array([trade.net_pnl for trade in trades], dtype=float)
        r_values = np.array([trade.r_multiple for trade in trades], dtype=float)
        wins = int(np.count_nonzero(net_values > 0))
        losses = int(np.count_nonzero(net_values <= 0))
        trade_count = len(trades)
        win_rate = wins / trade_count * 100.0 if trade_count else 0.0
        gross_profit = float(net_values[net_values > 0].sum()) if trade_count else 0.0
        gross_loss = float(-net_values[net_values < 0].sum()) if trade_count else 0.0
        if gross_loss > 0:
            profit_factor: float | None = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = None
        else:
            profit_factor = 0.0
        expectancy = float(r_values.mean()) if trade_count else 0.0

        curve_values = np.array([value for _, value in equity_curve], dtype=float)
        if len(curve_values):
            running_peak = np.maximum.accumulate(curve_values)
            drawdowns = np.where(
                running_peak > 0,
                (running_peak - curve_values) / running_peak * 100.0,
                0.0,
            )
            max_drawdown = float(np.nanmax(drawdowns))
        else:
            max_drawdown = 0.0

        return BacktestResult(
            symbol=symbol,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            net_pnl=net_pnl,
            total_return_percent=total_return,
            max_drawdown_percent=max_drawdown,
            trades=trade_count,
            wins=wins,
            losses=losses,
            win_rate_percent=win_rate,
            profit_factor=profit_factor,
            expectancy_r=expectancy,
            average_r=expectancy,
            trades_detail=tuple(trades),
            equity_curve=tuple(equity_curve),
        )
