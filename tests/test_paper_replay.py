import pandas as pd

from trading_bot.paper import PaperBroker, run_paper_replay


def make_data(rows=340):
    prices = [100 + i * 0.35 for i in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01",
                periods=rows,
                freq="1h",
                tz="UTC",
            ),
            "open": prices,
            "high": [price + 1.25 for price in prices],
            "low": [price - 1.25 for price in prices],
            "close": prices,
            "volume": [1200.0] * rows,
        }
    )


def test_paper_replay_runs_shared_decision_engine():
    broker = PaperBroker(
        starting_equity=10_000,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    run_paper_replay(
        data=make_data(),
        symbol="BTC/USDT",
        timeframe="1h",
        broker=broker,
        risk_percent=0.01,
        min_confirmed_score=50,
        allow_short=False,
    )

    assert len(broker.completed_trades) > 0
    assert {
        trade.strategy
        for trade in broker.completed_trades
    } == {"trend_continuation"}
    assert broker.snapshot().open_positions == 0
    assert broker.snapshot().pending_orders == 0
