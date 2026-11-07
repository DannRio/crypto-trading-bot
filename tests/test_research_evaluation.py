import pandas as pd

from trading_bot.research import (
    run_out_of_sample_evaluation,
    run_walk_forward_evaluation,
)


def make_data(rows=720):
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


def backtest_kwargs():
    return {
        "initial_equity": 10_000,
        "risk_percent": 0.01,
        "min_confirmed_score": 50,
        "fee_rate": 0.0,
        "slippage_bps": 0.0,
        "allow_short": False,
    }


def test_out_of_sample_evaluation_has_three_periods():
    data = make_data()
    train_cutoff = data["timestamp"].iloc[300]
    validation_cutoff = data["timestamp"].iloc[500]

    summary, details = run_out_of_sample_evaluation(
        data,
        train_cutoff=train_cutoff,
        validation_cutoff=validation_cutoff,
        **backtest_kwargs(),
    )

    assert list(summary["segment"]) == [
        "TRAIN",
        "VALIDATION",
        "TEST",
    ]
    assert set(details) == {"TRAIN", "VALIDATION", "TEST"}
    assert summary["rows"].sum() == len(data)


def test_walk_forward_evaluation_produces_rolling_windows():
    summary, details = run_walk_forward_evaluation(
        make_data(),
        train_bars=300,
        test_bars=150,
        step_bars=150,
        **backtest_kwargs(),
    )

    assert len(summary) == 2
    assert len(details) == 2
    assert "test_return_pct" in summary.columns
    assert "test_average_r" in summary.columns
