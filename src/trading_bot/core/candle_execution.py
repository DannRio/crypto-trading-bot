"""Deterministic OHLC path replay shared by research and simulated paper execution.

The two canonical paths are sensitivity assumptions, NOT tick reconstruction
or mathematical bounds on every possible intrabar path.
"""
from trading_bot.core.enums import Signal


def economic_break_even(entry, signal, fee_rate, slippage_bps, mode="cost"):
    if mode == "price":
        return entry
    slip = slippage_bps / 10000
    if signal == Signal.LONG:
        return entry * (1 + fee_rate) / ((1 - slip) * (1 - fee_rate))
    return entry * (1 - fee_rate) / ((1 + slip) * (1 + fee_rate))


def replay_price_path(points, *, get_position, get_pending, fill_entry,
                      cancel_pending, take_tp1, close_position, observe=lambda price: None):
    """Callbacks expose current state after each event; old extrema are never reused."""
    opening = points[0]
    pos = get_position()
    if pos is not None:
        direction = 1 if pos.signal == Signal.LONG else -1
        observe(opening)
        if direction * (opening - pos.effective_stop) <= 0:
            close_position(opening, "STOP_LOSS_AFTER_TP1" if pos.tp1_hit else "STOP_LOSS")
        else:
            if direction * (opening - pos.take_profit_1) >= 0:
                take_tp1()
            pos = get_position()
            if pos is not None and direction * (opening - pos.take_profit_2) >= 0:
                close_position(pos.take_profit_2, "TAKE_PROFIT_2_AFTER_TP1" if pos.tp1_hit else "TAKE_PROFIT_2")
    order = get_pending()
    if order is not None:
        direction = 1 if order.signal == Signal.LONG else -1
        if direction * (opening - order.stop_loss) <= 0:
            cancel_pending()
        elif order.entry_low <= opening <= order.entry_high:
            fill_entry(opening)
    for start, end in zip(points, points[1:]):
        order = get_pending()
        if get_position() is None and order is not None:
            entry = order.entry_high if start > order.entry_high else order.entry_low
            if min(start, end) <= entry <= max(start, end):
                fill_entry(entry)
                start = entry
            elif min(start, end) <= order.stop_loss <= max(start, end):
                cancel_pending()
        pos = get_position()
        if pos is None:
            continue
        direction = 1 if pos.signal == Signal.LONG else -1
        if direction * (end - start) < 0 and min(start, end) <= pos.effective_stop <= max(start, end):
            observe(pos.effective_stop)
            close_position(pos.effective_stop, "STOP_LOSS_AFTER_TP1" if pos.tp1_hit else "STOP_LOSS")
            continue
        if direction * (end - start) > 0:
            if min(start, end) <= pos.take_profit_1 <= max(start, end):
                observe(pos.take_profit_1)
                take_tp1()
            pos = get_position()
            if pos is not None and min(start, end) <= pos.take_profit_2 <= max(start, end):
                observe(pos.take_profit_2)
                close_position(pos.take_profit_2, "TAKE_PROFIT_2_AFTER_TP1" if pos.tp1_hit else "TAKE_PROFIT_2")
                continue
        observe(end)


def ohlc_paths(candle, order=None, position=None):
    """Include a feasible TP1-then-reversal path, not only the two extrema orders.

    Without this extra path, both canonical extrema paths can hit TP2 even
    though a break-even stop could have been hit between TP1 and TP2.
    """
    opening, high, low, close = (float(candle.get(k, candle["close"])) for k in ("open", "high", "low", "close"))
    paths = [[opening, high, low, close], [opening, low, high, close]]
    subject = position if position is not None else order
    if subject is not None and low <= subject.take_profit_1 <= high:
        adverse, favorable = (low, high) if subject.signal == Signal.LONG else (high, low)
        prefix = [opening]
        if position is None and order is not None:
            entry = min(max(opening, order.entry_low), order.entry_high)
            if not low <= entry <= high:
                return paths
            prefix.append(entry)
        paths.append([*prefix, subject.take_profit_1, adverse, favorable, close])
    return paths
