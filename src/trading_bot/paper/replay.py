import pandas as pd

from trading_bot.analysis import decision_indicators
from trading_bot.config import INDICATOR_WINDOW
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.decision import DecisionEngine
from trading_bot.paper.broker import PaperBroker
from trading_bot.strategies import DEFAULT_STRATEGIES


def run_paper_replay(
    data: pd.DataFrame,
    symbol: str,
    timeframe: str,
    broker: PaperBroker,
    risk_percent: float = 0.01,
    min_confirmed_score: int = 70,
    warmup_candles: int = 220,
    decision_lookback: int = INDICATOR_WINDOW,
    allow_short: bool = True,
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
) -> PaperBroker:
    if type(decision_lookback) is not int or decision_lookback < 21:
        raise ValueError("decision_lookback must be an integer of at least 21 candles.")
    prepared = data.copy()

    for i in range(
        max(warmup_candles, 1),
        len(prepared),
    ):
        candle = prepared.iloc[i]

        broker.process_candle(
            symbol=symbol,
            timeframe=timeframe,
            candle=candle,
        )

        if i >= len(prepared) - 1:
            continue

        history = decision_indicators(prepared.iloc[
            max(0, i - decision_lookback + 1): i + 1
        ], window=decision_lookback)
        decision = DecisionEngine(
            account_equity=broker.account_equity(),
            risk_percent=risk_percent,
            min_confirmed_score=min_confirmed_score,
            strategy_names=strategy_names, allow_short=allow_short,
            fee_rate=broker.fee_rate, slippage_bps=broker.slippage_bps,
        ).evaluate(history)

        # Revalidate unfilled orders at candle close. A pending thesis may
        # survive only while the same strategy and direction remain confirmed.
        for order in list(broker.pending_orders):
            if (
                order.symbol != symbol
                or order.timeframe != timeframe
            ):
                continue
            still_valid = (
                decision.assessment.status
                == SetupStatus.CONFIRMED
                and decision.setup is not None
                and decision.assessment.signal
                == order.signal
                and decision.assessment.strategy
                == order.strategy
            )
            if order.event_id:
                direction = 1 if order.signal == Signal.LONG else -1
                cancel = order.cancel_price if order.cancel_price is not None else order.stop_loss
                expected_regime = "RANGE" if order.setup_type == "range_reversal" else ("BULL_TREND" if direction == 1 else "BEAR_TREND")
                still_valid = direction * (float(candle["close"]) - cancel) > 0 and decision.structure.regime.value == expected_regime
            if not still_valid:
                broker.cancel_order(
                    order.order_id,
                    reason="SETUP_INVALIDATED",
                )

        if broker.has_exposure(symbol, timeframe):
            continue

        if (
            decision.assessment.status
            != SetupStatus.CONFIRMED
            or decision.setup is None
        ):
            continue

        if (
            not allow_short
            and decision.assessment.signal == Signal.SHORT
        ):
            continue

        if broker.has_setup_event(symbol, timeframe, decision.setup.event_id):
            continue
        broker.submit_setup(
            symbol=symbol,
            timeframe=timeframe,
            setup=decision.setup,
            created_at=candle["timestamp"],
            regime=decision.structure.regime.value,
            strategy=decision.assessment.strategy,
        )

    if not prepared.empty:
        last = prepared.iloc[-1]
        broker.last_prices[symbol] = float(last["close"])
        broker.close_all(
            timestamp=last["timestamp"],
            reason="END_OF_REPLAY",
        )
        broker.cancel_all_pending(
            reason="END_OF_REPLAY",
        )

    return broker
