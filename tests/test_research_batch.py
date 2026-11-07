import pandas as pd

from trading_bot.data import HistoricalDataStore
from trading_bot.research import DatasetSpec, run_batch_research


def make_data(rows=320):
    prices = [100 + i * 0.5 for i in range(rows)]
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


def test_batch_research_uses_local_history(tmp_path):
    store = HistoricalDataStore(tmp_path)
    store.upsert(
        "kucoin",
        "spot",
        "BTC/USDT",
        "1h",
        make_data(),
    )

    summary, details = run_batch_research(
        specs=[
            DatasetSpec(
                exchange_id="kucoin",
                market_type="spot",
                symbol="BTC/USDT",
                timeframe="1h",
            )
        ],
        store=store,
        min_confirmed_score=50,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert len(summary) == 1
    assert summary.iloc[0]["status"] == "OK"
    assert summary.iloc[0]["rows"] == 320
    assert summary.iloc[0]["trades"] > 0
    assert len(details) == 1
