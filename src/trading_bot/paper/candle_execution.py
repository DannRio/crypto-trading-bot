"""Paper-broker adapter for the same ordered OHLC paths used by the backtester."""
from copy import copy, deepcopy
from types import SimpleNamespace

import pandas as pd

from trading_bot.core.candle_execution import replay_price_path, ohlc_paths
from trading_bot.core.enums import Signal
from trading_bot.paper.models import PaperOrderStatus


def process_broker_candle(broker, symbol, timeframe, candle):
    stamp = pd.Timestamp(candle["timestamp"])
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    key = (symbol, timeframe)
    broker.last_prices[symbol] = float(candle["close"])
    def available(order):
        created = pd.Timestamp(order.created_at)
        created = created.tz_localize("UTC") if created.tzinfo is None else created.tz_convert("UTC")
        return stamp >= created
    # A completed OHLC bar includes prices that happened before an order placed
    # partway through it. Wait for a complete post-submission bar rather than
    # inventing a fill or consuming entry expiry from that unavailable interval.
    relevant = {o.order_id for o in broker.pending_orders
                if (o.symbol, o.timeframe) == key and available(o)}
    if key in broker.positions:
        relevant.add(broker.positions[key].order_id)
    if not relevant:
        return []
    candidates = []
    original_order = next((o for o in broker.pending_orders
                           if (o.symbol, o.timeframe) == key and available(o)), None)
    for points in ohlc_paths(candle, original_order, broker.positions.get(key)):
        trial = copy(broker)
        trial.journal = None  # speculative paths must never write persistent state
        trial.orders = dict(broker.orders)
        for order_id in relevant:
            trial.orders[order_id] = deepcopy(broker.orders[order_id])
        trial.positions = deepcopy(broker.positions)
        trial.completed_trades = []
        trial.last_prices = dict(broker.last_prices)
        def get_pending():
            return next((o for o in trial.pending_orders
                         if (o.symbol, o.timeframe) == key and available(o)), None)
        def get_position():
            pos = trial.positions.get(key)
            if pos is None:
                return None
            return SimpleNamespace(signal=pos.signal, effective_stop=pos.effective_stop if pos.effective_stop is not None else pos.stop_loss,
                                   take_profit_1=pos.take_profit_1, take_profit_2=pos.take_profit_2, tp1_hit=pos.tp1_hit)
        def fill(price):
            order = get_pending()
            direction = 1 if order.signal == Signal.LONG else -1
            executed = price * (1 + direction * trial.slippage_bps / 10000)
            if direction * (executed - order.stop_loss) <= 0 or direction * (order.take_profit_1 - executed) <= 0:
                trial.cancel_order(order.order_id, "INVALID_FILL_GEOMETRY")
            else:
                trial._fill_order(order, stamp, price)
        def cancel():
            order = get_pending()
            if order:
                trial.cancel_order(order.order_id, "SETUP_INVALIDATED_BEFORE_ENTRY")
        replay_price_path(points, get_position=get_position, get_pending=get_pending,
                          fill_entry=fill, cancel_pending=cancel,
                          take_tp1=lambda: trial._take_tp1(key),
                          close_position=lambda price, reason: trial._close_position(key, price, stamp, reason))
        remaining = get_pending()
        if remaining is not None:
            remaining.bars_remaining -= 1
            if remaining.bars_remaining <= 0:
                remaining.status = PaperOrderStatus.EXPIRED
                remaining.exit_reason = "ENTRY_EXPIRED"
        pos = trial.positions.get(key)
        fingerprint = (round(trial.realized_net_pnl, 10),
                       None if pos is None else (pos.position_size, pos.effective_stop, pos.tp1_hit),
                       tuple((t.exit_reason, round(t.net_pnl, 10)) for t in trial.completed_trades),
                       remaining is not None)
        candidates.append((trial.account_equity(), trial, fingerprint))
    ambiguous = len({candidate[2] for candidate in candidates}) > 1
    _, chosen, _ = min(candidates, key=lambda item: item[0])
    for order_id in relevant:
        # Preserve references held by callers; only commit the selected path.
        broker.orders[order_id].__dict__.update(chosen.orders[order_id].__dict__)
    broker.positions = chosen.positions
    broker.realized_net_pnl = chosen.realized_net_pnl
    broker.fees_paid = chosen.fees_paid
    broker.ambiguous_bars += int(ambiguous)
    broker.completed_trades.extend(chosen.completed_trades)
    if broker.journal:
        for order_id in relevant:
            broker.journal.save_order(broker.orders[order_id])
        for trade in chosen.completed_trades:
            broker.journal.save_trade(trade)
    return chosen.completed_trades
