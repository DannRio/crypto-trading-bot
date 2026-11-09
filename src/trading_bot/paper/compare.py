from dataclasses import dataclass

import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.paper.broker import PaperBroker
from trading_bot.paper.replay import run_paper_replay
from trading_bot.strategies import DEFAULT_STRATEGIES


@dataclass(frozen=True)
class PaperBacktestComparison:
    backtest_final_equity: float
    paper_final_equity: float
    final_equity_delta: float
    backtest_return_pct: float
    paper_return_pct: float
    return_delta_pct: float
    backtest_trades: int
    paper_trades: int
    trade_count_delta: int


def compare_paper_to_backtest(
    data: pd.DataFrame,
    symbol: str,
    timeframe: str,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    min_confirmed_score: int = 70,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    allow_short: bool = True,
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
) -> PaperBacktestComparison:
    backtest = run_decision_backtest(
        data=data,
        initial_equity=initial_equity,
        risk_percent=risk_percent,
        min_confirmed_score=min_confirmed_score,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        allow_short=allow_short,
        strategy_names=strategy_names,
    )

    broker = PaperBroker(
        starting_equity=initial_equity,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    run_paper_replay(
        data=data,
        symbol=symbol,
        timeframe=timeframe,
        broker=broker,
        risk_percent=risk_percent,
        min_confirmed_score=min_confirmed_score,
        allow_short=allow_short,
        strategy_names=strategy_names,
    )

    snapshot = broker.snapshot()
    paper_return = (
        (snapshot.equity / initial_equity) - 1
    ) * 100

    return PaperBacktestComparison(
        backtest_final_equity=backtest.final_equity,
        paper_final_equity=snapshot.equity,
        final_equity_delta=(
            snapshot.equity - backtest.final_equity
        ),
        backtest_return_pct=backtest.total_return_pct,
        paper_return_pct=paper_return,
        return_delta_pct=(
            paper_return - backtest.total_return_pct
        ),
        backtest_trades=backtest.total_trades,
        paper_trades=len(broker.completed_trades),
        trade_count_delta=(
            len(broker.completed_trades)
            - backtest.total_trades
        ),
    )
