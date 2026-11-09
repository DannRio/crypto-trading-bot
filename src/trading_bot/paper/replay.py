import pandas as pd

from trading_bot.analysis import add_indicators
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
    decision_lookback: int = 250,
    allow_short: bool = True,
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
) -> PaperBroker:
    prepared = add_indicators(data.copy())

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

        if (
            i >= len(prepared) - 1
            or broker.has_exposure(symbol, timeframe)
        ):
            continue

        history = prepared.iloc[
            max(0, i - decision_lookback + 1): i + 1
        ]
        decision = DecisionEngine(
            account_equity=broker.account_equity(),
            risk_percent=risk_percent,
            min_confirmed_score=min_confirmed_score,
            strategy_names=strategy_names,
        ).evaluate(history)

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
