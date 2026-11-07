import pandas as pd

from trading_bot.backtesting import run_decision_backtest


def trending_candles(rows=320):
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


def test_decision_backtest_records_strategy_diagnostics():
    result = run_decision_backtest(
        trending_candles(),
        min_confirmed_score=50,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert result.total_trades > 0
    assert "strategy" in result.trades.columns
    assert "score" in result.trades.columns
    assert "r_multiple" in result.trades.columns
    assert set(result.trades["signal"]) == {"LONG"}
