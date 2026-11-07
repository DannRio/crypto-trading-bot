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
)
from trading_bot.planning import TradeSetup


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


class PaperBroker:
    def __init__(
        self,
        starting_equity: float = 10_000,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        risk_config: PortfolioRiskConfig | None = None,
        journal: PaperJournal | None = None,
    ):
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive.")
        if fee_rate < 0 or slippage_bps < 0:
            raise ValueError("Fees and slippage cannot be negative.")

        self.starting_equity = starting_equity
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.risk_manager = PortfolioRiskManager(risk_config)
        self.journal = journal

        self.orders: dict[str, PaperOrder] = {}
        self.positions: dict[tuple[str, str], PaperPosition] = {}
        self.completed_trades: list[PaperTrade] = []
        self.last_prices: dict[str, float] = {}

        self.realized_net_pnl = 0.0
        self.fees_paid = 0.0

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

    def submit_setup(
        self,
        symbol: str,
        timeframe: str,
        setup: TradeSetup,
        created_at,
        regime: str = "",
        expires_after_bars: int = 3,
    ) -> PaperOrder:
        if setup.status != SetupStatus.CONFIRMED:
            raise ValueError("Only confirmed setups can be submitted.")
        if expires_after_bars <= 0:
            raise ValueError("expires_after_bars must be positive.")
        if self.has_exposure(symbol, timeframe):
            raise ValueError(
                f"Exposure already exists for {symbol} {timeframe}."
            )

        check = self.risk_manager.check_new_order(
            account_equity=self.account_equity(),
            symbol=symbol,
            new_risk=setup.actual_risk_amount,
            positions=list(self.positions.values()),
            pending_orders=self.pending_orders,
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
            stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1,
            take_profit_2=setup.take_profit_2,
            position_size=setup.position_size,
            planned_risk=setup.actual_risk_amount,
            score=setup.score,
            regime=regime,
            bars_remaining=expires_after_bars,
        )
        self.orders[order.order_id] = order
        if self.journal:
            self.journal.save_order(order)
        return order

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

    def cancel_all_pending(
        self,
        reason: str = "CANCELLED",
    ) -> None:
        for order in list(self.pending_orders):
            self.cancel_order(order.order_id, reason=reason)

    def _fill_order(
        self,
        order: PaperOrder,
        timestamp: pd.Timestamp,
    ) -> PaperPosition:
        fill_price = _entry_with_slippage(
            order.reference_entry,
            order.signal,
            self.slippage_bps,
        )
        entry_fee = (
            fill_price
            * order.position_size
            * self.fee_rate
        )
        actual_risk = (
            abs(fill_price - order.stop_loss)
            * order.position_size
        )

        self.realized_net_pnl -= entry_fee
        self.fees_paid += entry_fee

        order.status = PaperOrderStatus.FILLED
        order.filled_at = timestamp
        order.fill_price = fill_price
        order.entry_fee = entry_fee

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
            risk_amount=actual_risk,
            entry_fee=entry_fee,
            score=order.score,
            regime=order.regime,
        )
        self.positions[(order.symbol, order.timeframe)] = position

        if self.journal:
            self.journal.save_order(order)
        return position

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
        gross_pnl = _gross_pnl(
            position.signal,
            position.entry_price,
            exit_price,
            position.position_size,
        )
        exit_fee = (
            exit_price
            * position.position_size
            * self.fee_rate
        )

        self.realized_net_pnl += gross_pnl - exit_fee
        self.fees_paid += exit_fee

        total_fees = position.entry_fee + exit_fee
        net_pnl = gross_pnl - total_fees
        r_multiple = (
            net_pnl / position.risk_amount
            if position.risk_amount > 0
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
            exit_price=exit_price,
            position_size=position.position_size,
            gross_pnl=gross_pnl,
            fees=total_fees,
            net_pnl=net_pnl,
            risk_amount=position.risk_amount,
            r_multiple=r_multiple,
            exit_reason=reason,
            score=position.score,
            regime=position.regime,
        )

        order = self.orders[position.order_id]
        order.status = PaperOrderStatus.CLOSED
        order.closed_at = timestamp
        order.exit_price = exit_price
        order.exit_reason = reason

        del self.positions[key]
        self.completed_trades.append(trade)

        if self.journal:
            self.journal.save_order(order)
            self.journal.save_trade(trade)
        return trade

    def process_candle(
        self,
        symbol: str,
        timeframe: str,
        candle,
    ) -> list[PaperTrade]:
        timestamp = _timestamp(candle["timestamp"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        self.last_prices[symbol] = close

        key = (symbol, timeframe)

        for order in list(self.pending_orders):
            if (
                order.symbol != symbol
                or order.timeframe != timeframe
            ):
                continue

            touched = (
                low
                <= order.reference_entry
                <= high
            )
            if touched:
                self._fill_order(order, timestamp)
            else:
                order.bars_remaining -= 1
                if order.bars_remaining <= 0:
                    order.status = PaperOrderStatus.EXPIRED
                    order.exit_reason = "ENTRY_EXPIRED"
                if self.journal:
                    self.journal.save_order(order)

        closed: list[PaperTrade] = []
        position = self.positions.get(key)
        if position is None:
            return closed

        if position.signal == Signal.LONG:
            hit_stop = low <= position.stop_loss
            hit_target = high >= position.take_profit_2
        else:
            hit_stop = high >= position.stop_loss
            hit_target = low <= position.take_profit_2

        if hit_stop:
            closed.append(
                self._close_position(
                    key,
                    position.stop_loss,
                    timestamp,
                    "STOP_LOSS",
                )
            )
        elif hit_target:
            closed.append(
                self._close_position(
                    key,
                    position.take_profit_2,
                    timestamp,
                    "TAKE_PROFIT_2",
                )
            )

        return closed

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
