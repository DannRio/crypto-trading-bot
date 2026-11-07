import pandas as pd
import pytest

from trading_bot.backtesting.metrics import (
    calculate_return_pct,
    calculate_trade_metrics,
)


def test_return_percentage():
    result = calculate_return_pct(
        initial_equity=10_000,
        final_equity=11_000,
    )

    assert result == pytest.approx(10.0)


def test_trade_metrics():
    trades = pd.DataFrame(
        {
            "pnl": [
                200.0,
                -100.0,
                200.0,
                -100.0,
            ]
        }
    )

    (
        total_trades,
        win_rate,
        profit_factor,
        average_pnl,
    ) = calculate_trade_metrics(trades)

    assert total_trades == 4
    assert win_rate == pytest.approx(50.0)
    assert profit_factor == pytest.approx(2.0)
    assert average_pnl == pytest.approx(50.0)


def test_empty_trade_metrics():
    trades = pd.DataFrame(
        columns=["pnl"]
    )

    result = calculate_trade_metrics(trades)

    assert result == (
        0,
        0.0,
        0.0,
        0.0,
    )