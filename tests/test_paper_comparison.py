import pandas as pd
import pytest

from trading_bot.paper import compare_paper_to_backtest


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


def test_paper_replay_matches_backtest_execution_model():
    comparison = compare_paper_to_backtest(
        data=make_data(),
        symbol="BTC/USDT",
        timeframe="1h",
        initial_equity=10_000,
        risk_percent=0.01,
        min_confirmed_score=50,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert comparison.trade_count_delta == 0
    assert comparison.final_equity_delta == pytest.approx(0.0)
    assert comparison.return_delta_pct == pytest.approx(0.0)
