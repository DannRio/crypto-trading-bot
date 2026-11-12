from dataclasses import replace
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import math
from trading_bot.core.candle_execution import economic_break_even
from trading_bot.paper.candle_execution import process_broker_candle
from uuid import uuid4

import pandas as pd

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.models import (
    PaperAccountSnapshot,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperTrade,
)
from trading_bot.paper.risk import (
    PortfolioRiskConfig,
    PortfolioRiskManager,
    PortfolioRiskState,
    RiskStateUpdate,
)
from trading_bot.planning import TradeSetup


def _atomic_method(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.atomic_transition():
            return method(self, *args, **kwargs)
    return wrapped


def _timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


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


def _entry_price_from_zone(
    order: PaperOrder,
    candle,
) -> float | None:
    zone_low = (
        order.entry_low
        if order.entry_low > 0
        else order.reference_entry
    )
    zone_high = (
        order.entry_high
        if order.entry_high > 0
        else order.reference_entry
    )
    low = float(candle["low"])
    high = float(candle["high"])
    if high < zone_low or low > zone_high:
        return None

    open_price = float(
        candle.get("open", candle["close"])
    )
    return min(max(open_price, zone_low), zone_high)


class PaperBroker:
    def __init__(
        self,
        starting_equity: float = 10_000,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        risk_config: PortfolioRiskConfig | None = None,
        journal: PaperJournal | None = None,
    ):
        if not math.isfinite(starting_equity) or starting_equity <= 0:
            raise ValueError("starting_equity must be positive.")
        if not (math.isfinite(fee_rate) and 0 <= fee_rate < 1
                and math.isfinite(slippage_bps) and 0 <= slippage_bps < 10000):
            raise ValueError("Fees and slippage must be finite and within supported bounds.")

        self.ambiguous_bars = 0
        self.starting_equity = starting_equity
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.risk_manager = PortfolioRiskManager(risk_config)
        self.journal = journal
        self._transition_depth = 0

        self.orders: dict[str, PaperOrder] = {}
        self.positions: dict[tuple[str, str], PaperPosition] = {}
        self.completed_trades: list[PaperTrade] = []
        self.last_prices: dict[str, float] = {}

        self.realized_net_pnl = 0.0
        self.fees_paid = 0.0
        self.historical_peak_equity = starting_equity
        self.risk_state: PortfolioRiskState | None = (
            journal.load_risk_state()
            if journal is not None
            else None
        )

    @contextmanager
    def atomic_transition(self):
        """Keep broker memory and its durable account on the same transition.

        The service may include a candle checkpoint in this scope. Speculative
        OHLC-path brokers have no journal and must not persist or nest commits.
        Closed orders/trades are immutable here, so only active order objects
        require a deep state snapshot.
        """
        if self.journal is None or self._transition_depth:
            yield self
            return
        order_refs = dict(self.orders)
        order_states = {key: deepcopy(order.__dict__) for key, order in self.orders.items()
                        if order.status in {PaperOrderStatus.PENDING, PaperOrderStatus.FILLED}}
        position_refs = dict(self.positions)
        position_states = {key: deepcopy(position.__dict__) for key, position in self.positions.items()}
        snapshot = {
            "completed_trades": list(self.completed_trades), "last_prices": dict(self.last_prices),
            "realized_net_pnl": self.realized_net_pnl, "fees_paid": self.fees_paid,
            "historical_peak_equity": self.historical_peak_equity,
            "risk_state": deepcopy(self.risk_state), "ambiguous_bars": self.ambiguous_bars,
        }
        self._transition_depth = 1
        try:
            with self.journal.transaction():
                yield self
        except BaseException:
            for key, state in order_states.items():
                order_refs[key].__dict__.clear()
                order_refs[key].__dict__.update(state)
            for key, state in position_states.items():
                position_refs[key].__dict__.clear()
                position_refs[key].__dict__.update(state)
            self.orders = order_refs
            self.positions = position_refs
            for key, value in snapshot.items():
                setattr(self, key, value)
            raise
        finally:
            self._transition_depth = 0



    @property
    def risk_halted(self) -> bool:
        return bool(
            self.risk_state is not None
            and self.risk_state.halted
        )

    @_atomic_method
    def evaluate_account_risk(
        self,
        timestamp,
    ) -> RiskStateUpdate:
        account_equity = self.account_equity()
        state = self.risk_state
        timestamp = _timestamp(timestamp)
        if state is not None:
            # Recovery can visit more than one symbol's backlog. Never move
            # portfolio day/week baselines back to a period already evaluated.
            timestamp = max(timestamp, _timestamp(state.evaluated_at))
        if state is None:
            state = self.risk_manager.initialize_state(
                account_equity=account_equity,
                timestamp=timestamp,
            )
            if self.historical_peak_equity > state.peak_equity:
                state = replace(
                    state,
                    peak_equity=self.historical_peak_equity,
                )

        update = self.risk_manager.update_state(
            state=state,
            account_equity=account_equity,
            timestamp=timestamp,
        )
        self.risk_state = update.state
        if self.journal is not None:
            self.journal.save_risk_state(update.state)
        return update

    @_atomic_method
    def reset_risk_halt(
        self,
        timestamp,
    ) -> PortfolioRiskState:
        state = self.risk_manager.reset_state(
            account_equity=self.account_equity(),
            timestamp=timestamp,
        )
        self.risk_state = state
        if self.journal is not None:
            self.journal.save_risk_state(state)
        return state


    @property
    def pending_orders(self) -> list[PaperOrder]:
        return [
            order
            for order in self.orders.values()
            if order.status == PaperOrderStatus.PENDING
        ]

    def has_exposure(
        self,
        symbol: str,
        timeframe: str,
    ) -> bool:
        key = (symbol, timeframe)
        if key in self.positions:
            return True
        return any(
            order.symbol == symbol
            and order.timeframe == timeframe
            and order.status == PaperOrderStatus.PENDING
            for order in self.orders.values()
        )

    def reserved_risk(self) -> float:
        return sum(
            position.risk_amount
            for position in self.positions.values()
        ) + sum(
            order.planned_risk
            for order in self.pending_orders
        )

    def _stop_risk(self, signal, entry_price, stop_price, quantity):
        direction = 1 if signal == Signal.LONG else -1
        stop_fill = _exit_with_slippage(stop_price, signal, self.slippage_bps)
        per_unit = direction * (entry_price - stop_fill) + self.fee_rate * (entry_price + stop_fill)
        # A profitable protected stop is not negative risk, and its absolute
        # distance from entry must not be counted as a new potential loss.
        return max(0.0, per_unit) * quantity

    def unrealized_pnl(self) -> float:
        total = 0.0
        for position in self.positions.values():
            mark = self.last_prices.get(position.symbol)
            if mark is None:
                continue
            total += _gross_pnl(
                position.signal,
                position.entry_price,
                mark,
                position.position_size,
            )
        return total

    def account_equity(self) -> float:
        return (
            self.starting_equity
            + self.realized_net_pnl
            + self.unrealized_pnl()
        )

    def snapshot(self) -> PaperAccountSnapshot:
        unrealized = self.unrealized_pnl()
        return PaperAccountSnapshot(
            starting_equity=self.starting_equity,
            equity=(
                self.starting_equity
                + self.realized_net_pnl
                + unrealized
            ),
            realized_net_pnl=self.realized_net_pnl,
            unrealized_pnl=unrealized,
            fees_paid=self.fees_paid,
            open_positions=len(self.positions),
            pending_orders=len(self.pending_orders),
            reserved_risk=self.reserved_risk(),
        )



    def restore_from_journal(self) -> dict[str, int]:
        if self.journal is None:
            raise ValueError(
                "A PaperJournal is required to restore broker state."
            )

        self.journal.verify_account_integrity()
        self.orders.clear()
        self.positions.clear()
        self.completed_trades.clear()
        self.last_prices.clear()
        self.risk_state = self.journal.load_risk_state()

        trades = self.journal.all_trades()
        self.realized_net_pnl = (
            float(trades["net_pnl"].sum())
            if not trades.empty
            else 0.0
        )
        realized_curve = self.starting_equity
        self.historical_peak_equity = self.starting_equity
        if not trades.empty:
            for pnl in trades["net_pnl"].astype(float):
                realized_curve += pnl
                self.historical_peak_equity = max(
                    self.historical_peak_equity,
                    realized_curve,
                )
        self.fees_paid = (
            float(trades["fees"].sum())
            if not trades.empty
            else 0.0
        )

        active = self.journal.active_orders()
        for row in active.itertuples(index=False):
            status = PaperOrderStatus(str(row.status))
            filled_at = (
                _timestamp(row.filled_at)
                if pd.notna(row.filled_at)
                else None
            )
            closed_at = (
                _timestamp(row.closed_at)
                if pd.notna(row.closed_at)
                else None
            )

            order = PaperOrder(
                order_id=str(row.order_id),
                symbol=str(row.symbol),
                timeframe=str(row.timeframe),
                signal=Signal(str(row.signal)),
                created_at=_timestamp(row.created_at),
                reference_entry=float(row.reference_entry),
                entry_low=(
                    float(row.entry_low)
                    if float(row.entry_low) > 0
                    else float(row.reference_entry)
                ),
                entry_high=(
                    float(row.entry_high)
                    if float(row.entry_high) > 0
                    else float(row.reference_entry)
                ),
                stop_loss=float(row.stop_loss),
                take_profit_1=float(row.take_profit_1),
                take_profit_2=float(row.take_profit_2),
                position_size=float(row.position_size),
                planned_risk=float(row.planned_risk),
                score=int(row.score),
                regime=str(row.regime),
                strategy=str(row.strategy),
                bars_remaining=int(row.bars_remaining),
                event_id=row.event_id if pd.notna(row.event_id) else None,
                setup_type=str(row.setup_type),
                cancel_price=float(row.cancel_price) if pd.notna(row.cancel_price) else None,
                break_even_mode=str(row.break_even_mode), risk_budget=float(row.risk_budget),
                status=status,
                filled_at=filled_at,
                fill_price=(
                    float(row.fill_price)
                    if pd.notna(row.fill_price)
                    else None
                ),
                entry_fee=float(row.entry_fee),
                closed_at=closed_at,
                exit_price=(
                    float(row.exit_price)
                    if pd.notna(row.exit_price)
                    else None
                ),
                exit_reason=(
                    str(row.exit_reason)
                    if pd.notna(row.exit_reason)
                    else None
                ),
                take_profit_1_fraction=float(
                    row.take_profit_1_fraction
                ),
                move_stop_to_break_even_after_tp1=bool(
                    row.move_stop_to_break_even_after_tp1
                ),
                tp1_hit=bool(row.tp1_hit),
                remaining_size=(
                    float(row.remaining_size)
                    if pd.notna(row.remaining_size)
                    else None
                ),
                realized_gross_pnl=float(
                    row.realized_gross_pnl
                ),
                realized_exit_fees=float(
                    row.realized_exit_fees
                ),
                realized_exit_value=float(
                    row.realized_exit_value
                ),
                effective_stop=(
                    float(row.effective_stop)
                    if pd.notna(row.effective_stop)
                    else None
                ),
            )
            self.orders[order.order_id] = order

            if status == PaperOrderStatus.FILLED:
                if order.fill_price is None or order.filled_at is None:
                    raise ValueError(
                        "Persisted FILLED order is missing fill data: "
                        f"{order.order_id}"
                    )
                initial_risk_amount = self._stop_risk(
                    order.signal, order.fill_price, order.stop_loss, order.position_size)
                remaining_size = (
                    order.remaining_size
                    if order.remaining_size is not None
                    else order.position_size
                )
                effective_stop = (
                    order.effective_stop
                    if order.effective_stop is not None
                    else order.stop_loss
                )
                current_risk = self._stop_risk(order.signal, order.fill_price, effective_stop, remaining_size)
                self.positions[
                    (order.symbol, order.timeframe)
                ] = PaperPosition(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    timeframe=order.timeframe,
                    signal=order.signal,
                    entry_time=order.filled_at,
                    entry_price=order.fill_price,
                    stop_loss=order.stop_loss,
                    take_profit_1=order.take_profit_1,
                    take_profit_2=order.take_profit_2,
                    position_size=remaining_size,
                    risk_amount=current_risk,
                    entry_fee=order.entry_fee,
                    score=order.score,
                    regime=order.regime,
                    strategy=order.strategy,
                    event_id=order.event_id, setup_type=order.setup_type, break_even_mode=order.break_even_mode,
                    initial_position_size=order.position_size,
                    initial_risk_amount=initial_risk_amount,
                    tp1_hit=order.tp1_hit,
                    take_profit_1_fraction=(
                        order.take_profit_1_fraction
                    ),
                    move_stop_to_break_even_after_tp1=(
                        order.move_stop_to_break_even_after_tp1
                    ),
                    effective_stop=effective_stop,
                    realized_gross_pnl=order.realized_gross_pnl,
                    realized_exit_fees=order.realized_exit_fees,
                    realized_exit_value=order.realized_exit_value,
                )
                self.realized_net_pnl += (
                    order.realized_gross_pnl
                    - order.realized_exit_fees
                    - order.entry_fee
                )
                self.fees_paid += (
                    order.entry_fee
                    + order.realized_exit_fees
                )

        return {
            "active_orders": len(active),
            "open_positions": len(self.positions),
            "completed_trades": len(trades),
        }


    @_atomic_method
    def submit_setup(
        self,
        symbol: str,
        timeframe: str,
        setup: TradeSetup,
        created_at,
        regime: str = "",
        strategy: str = "trend_continuation",
        expires_after_bars: int = 3,
        correlations: dict[str, float] | None = None,
    ) -> PaperOrder:
        if setup.status != SetupStatus.CONFIRMED:
            raise ValueError("Only confirmed setups can be submitted.")
        if expires_after_bars <= 0:
            raise ValueError("expires_after_bars must be positive.")
        if self.has_exposure(symbol, timeframe):
            raise ValueError(
                f"Exposure already exists for {symbol} {timeframe}."
            )

        if self.has_setup_event(symbol, timeframe, setup.event_id):
            raise ValueError("Setup event already consumed; a fresh event is required.")
        check = self.risk_manager.check_new_order(
            account_equity=self.account_equity(),
            symbol=symbol,
            signal=setup.signal,
            new_risk=setup.actual_risk_amount,
            new_notional=setup.notional_value,
            positions=list(self.positions.values()),
            pending_orders=self.pending_orders,
            correlations=correlations,
            risk_halted=self.risk_halted,
        )
        if not check.allowed:
            raise ValueError(check.reason)

        order = PaperOrder(
            order_id=uuid4().hex,
            symbol=symbol,
            timeframe=timeframe,
            signal=setup.signal,
            created_at=_timestamp(created_at),
            reference_entry=setup.reference_entry,
            entry_low=setup.entry_low,
            entry_high=setup.entry_high,
            stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1,
            take_profit_2=setup.take_profit_2,
            position_size=setup.position_size,
            planned_risk=setup.actual_risk_amount,
            score=setup.score,
            regime=regime,
            strategy=strategy,
            bars_remaining=expires_after_bars,
            event_id=setup.event_id, setup_type=setup.setup_type, cancel_price=setup.cancel_price,
            break_even_mode=setup.break_even_mode, risk_budget=setup.target_risk_amount,
            take_profit_1_fraction=(
                setup.take_profit_1_fraction
            ),
            move_stop_to_break_even_after_tp1=(
                setup.move_stop_to_break_even_after_tp1
            ),
        )
        self.orders[order.order_id] = order
        if self.journal:
            self.journal.save_order(order)
        return order

    def has_setup_event(self, symbol, timeframe, event_id):
        if not event_id:
            return False
        if any(o.symbol == symbol and o.timeframe == timeframe and o.event_id == event_id for o in self.orders.values()):
            return True
        return bool(self.journal and self.journal.has_setup_event(symbol, timeframe, event_id))

    @_atomic_method
    def cancel_order(
        self,
        order_id: str,
        reason: str = "CANCELLED",
    ) -> None:
        order = self.orders[order_id]
        if order.status != PaperOrderStatus.PENDING:
            return
        order.status = PaperOrderStatus.CANCELLED
        order.exit_reason = reason
        if self.journal:
            self.journal.save_order(order)

    @_atomic_method
    def cancel_all_pending(
        self,
        reason: str = "CANCELLED",
    ) -> None:
        for order in list(self.pending_orders):
            self.cancel_order(order.order_id, reason=reason)

    @_atomic_method
    def _fill_order(
        self,
        order: PaperOrder,
        timestamp: pd.Timestamp,
        raw_entry_price: float,
    ) -> PaperPosition:
        fill_price = _entry_with_slippage(
            raw_entry_price,
            order.signal,
            self.slippage_bps,
        )
        direction = 1 if order.signal == Signal.LONG else -1
        stop_fill = _exit_with_slippage(order.stop_loss, order.signal, self.slippage_bps)
        loss_per_unit = direction * (fill_price - stop_fill) + self.fee_rate * (fill_price + stop_fill)
        if not math.isfinite(loss_per_unit) or loss_per_unit <= 0:
            raise ValueError("The proposed fill has no finite positive modeled stop loss.")
        budget = order.risk_budget if order.risk_budget > 0 else order.planned_risk
        order.position_size = min(order.position_size, budget / loss_per_unit,
                                  self.account_equity() / (fill_price * (1 + self.fee_rate)))
        entry_fee = (
            fill_price
            * order.position_size
            * self.fee_rate
        )
        initial_risk = loss_per_unit * order.position_size

        self.realized_net_pnl -= entry_fee
        self.fees_paid += entry_fee

        order.status = PaperOrderStatus.FILLED
        order.filled_at = timestamp
        order.fill_price = fill_price
        order.entry_fee = entry_fee
        order.remaining_size = order.position_size
        order.effective_stop = order.stop_loss

        position = PaperPosition(
            order_id=order.order_id,
            symbol=order.symbol,
            timeframe=order.timeframe,
            signal=order.signal,
            entry_time=timestamp,
            entry_price=fill_price,
            stop_loss=order.stop_loss,
            take_profit_1=order.take_profit_1,
            take_profit_2=order.take_profit_2,
            position_size=order.position_size,
            risk_amount=initial_risk,
            entry_fee=entry_fee,
            score=order.score,
            regime=order.regime,
            strategy=order.strategy,
            event_id=order.event_id, setup_type=order.setup_type, break_even_mode=order.break_even_mode,
            initial_position_size=order.position_size,
            initial_risk_amount=initial_risk,
            tp1_hit=False,
            take_profit_1_fraction=(
                order.take_profit_1_fraction
            ),
            move_stop_to_break_even_after_tp1=(
                order.move_stop_to_break_even_after_tp1
            ),
            effective_stop=order.stop_loss,
        )
        self.positions[(order.symbol, order.timeframe)] = position

        if self.journal:
            self.journal.save_order(order)
        return position

    @_atomic_method
    def _take_tp1(
        self,
        key: tuple[str, str],
    ) -> None:
        position = self.positions[key]
        if (
            position.tp1_hit
            or position.take_profit_1_fraction <= 0
        ):
            return

        exit_size = min(
            position.initial_position_size
            * position.take_profit_1_fraction,
            position.position_size,
        )
        if exit_size <= 0:
            return

        exit_price = _exit_with_slippage(
            position.take_profit_1,
            position.signal,
            self.slippage_bps,
        )
        gross_pnl = _gross_pnl(
            position.signal,
            position.entry_price,
            exit_price,
            exit_size,
        )
        exit_fee = (
            exit_price
            * exit_size
            * self.fee_rate
        )

        self.realized_net_pnl += gross_pnl - exit_fee
        self.fees_paid += exit_fee

        position.position_size -= exit_size
        position.realized_gross_pnl += gross_pnl
        position.realized_exit_fees += exit_fee
        position.realized_exit_value += (
            exit_price * exit_size
        )
        position.tp1_hit = True

        if position.move_stop_to_break_even_after_tp1:
            stop = economic_break_even(position.entry_price, position.signal, self.fee_rate,
                                       self.slippage_bps, position.break_even_mode)
            if ((position.signal == Signal.LONG and stop < position.take_profit_1)
                    or (position.signal == Signal.SHORT and stop > position.take_profit_1)):
                position.effective_stop = stop

        position.risk_amount = self._stop_risk(
            position.signal, position.entry_price,
            position.effective_stop if position.effective_stop is not None else position.stop_loss,
            position.position_size)

        order = self.orders[position.order_id]
        order.tp1_hit = True
        order.remaining_size = position.position_size
        order.realized_gross_pnl = (
            position.realized_gross_pnl
        )
        order.realized_exit_fees = (
            position.realized_exit_fees
        )
        order.realized_exit_value = (
            position.realized_exit_value
        )
        order.effective_stop = position.effective_stop
        if self.journal:
            self.journal.save_order(order)

    @_atomic_method
    def _close_position(
        self,
        key: tuple[str, str],
        raw_exit_price: float,
        timestamp: pd.Timestamp,
        reason: str,
    ) -> PaperTrade:
        position = self.positions[key]
        exit_price = _exit_with_slippage(
            raw_exit_price,
            position.signal,
            self.slippage_bps,
        )
        remaining_size = position.position_size
        final_gross_pnl = _gross_pnl(
            position.signal,
            position.entry_price,
            exit_price,
            remaining_size,
        )
        exit_fee = (
            exit_price
            * remaining_size
            * self.fee_rate
        )

        self.realized_net_pnl += (
            final_gross_pnl - exit_fee
        )
        self.fees_paid += exit_fee

        total_gross_pnl = (
            position.realized_gross_pnl
            + final_gross_pnl
        )
        total_fees = (
            position.entry_fee
            + position.realized_exit_fees
            + exit_fee
        )
        net_pnl = total_gross_pnl - total_fees

        initial_size = (
            position.initial_position_size
            if position.initial_position_size > 0
            else remaining_size
        )
        weighted_exit_value = (
            position.realized_exit_value
            + exit_price * remaining_size
        )
        average_exit = (
            weighted_exit_value / initial_size
            if initial_size > 0
            else exit_price
        )
        initial_risk = (
            position.initial_risk_amount
            if position.initial_risk_amount > 0
            else position.risk_amount
        )
        r_multiple = (
            net_pnl / initial_risk
            if initial_risk > 0
            else 0.0
        )

        trade = PaperTrade(
            order_id=position.order_id,
            symbol=position.symbol,
            timeframe=position.timeframe,
            signal=position.signal,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=average_exit,
            position_size=initial_size,
            gross_pnl=total_gross_pnl,
            fees=total_fees,
            net_pnl=net_pnl,
            risk_amount=initial_risk,
            r_multiple=r_multiple,
            exit_reason=reason,
            score=position.score,
            regime=position.regime,
            strategy=position.strategy,
            tp1_hit=position.tp1_hit, event_id=position.event_id, setup_type=position.setup_type,
        )

        order = self.orders[position.order_id]
        order.status = PaperOrderStatus.CLOSED
        order.closed_at = timestamp
        order.exit_price = average_exit
        order.exit_reason = reason
        order.tp1_hit = position.tp1_hit
        order.remaining_size = 0.0
        order.realized_gross_pnl = (
            position.realized_gross_pnl
        )
        order.realized_exit_fees = (
            position.realized_exit_fees
        )
        order.realized_exit_value = (
            position.realized_exit_value
        )
        order.effective_stop = position.effective_stop

        del self.positions[key]
        self.completed_trades.append(trade)

        if self.journal:
            self.journal.save_order(order)
            self.journal.save_trade(trade)
        return trade

    @_atomic_method
    def process_candle(
        self,
        symbol: str,
        timeframe: str,
        candle,
    ) -> list[PaperTrade]:
        return process_broker_candle(self, symbol, timeframe, candle)

    @_atomic_method
    def close_all(
        self,
        timestamp,
        reason: str = "MANUAL_CLOSE",
    ) -> list[PaperTrade]:
        timestamp = _timestamp(timestamp)
        trades: list[PaperTrade] = []

        for key, position in list(self.positions.items()):
            mark = self.last_prices.get(position.symbol)
            if mark is None:
                continue
            trades.append(
                self._close_position(
                    key,
                    mark,
                    timestamp,
                    reason,
                )
            )

        return trades
