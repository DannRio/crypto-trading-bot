import json

import pandas as pd
import pytest

from trading_bot.research import (
    expand_parameter_grid,
    run_parameter_experiments,
    training_slice,
)


def make_data(rows=420):
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


def test_grid_expansion_is_deterministic_and_capped():
    grid = {
        "planner.reward_to_risk_2": [1.5, 2.0],
        "min_confirmed_score": [60, 70],
    }

    combinations = expand_parameter_grid(grid)

    assert len(combinations) == 4
    assert combinations[0] == {
        "min_confirmed_score": 60,
        "planner.reward_to_risk_2": 1.5,
    }

    with pytest.raises(ValueError, match="limit"):
        expand_parameter_grid(
            {
                "a": list(range(30)),
                "b": list(range(30)),
            },
            max_combinations=500,
        )


def test_training_slice_excludes_cutoff_and_future_rows():
    data = make_data()
    cutoff = data["timestamp"].iloc[300]

    training = training_slice(
        data,
        train_end=cutoff,
    )

    assert len(training) == 300
    assert training["timestamp"].max() < cutoff


def test_parameter_experiments_record_full_configuration():
    data = make_data()
    cutoff = data["timestamp"].iloc[360]

    results = run_parameter_experiments(
        data=data,
        train_end=cutoff,
        grid={
            "min_confirmed_score": [50, 70],
            "planner.reward_to_risk_2": [1.5, 2.0],
        },
        strategy_names=("trend_continuation",),
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert len(results) == 4
    assert results["experiment_id"].is_unique
    assert set(results["training_rows"]) == {360}
    assert all(
        timestamp == cutoff
        for timestamp in results["train_end_exclusive"]
    )

    configs = [
        json.loads(value)
        for value in results["effective_config_json"]
    ]
    assert {
        config["trade_plan_config"][
            "reward_to_risk_2"
        ]
        for config in configs
    } == {1.5, 2.0}


def test_invalid_parameter_path_is_rejected():
    with pytest.raises(ValueError, match="Unsupported parameter path"):
        run_parameter_experiments(
            data=make_data(),
            train_end="2025-01-15",
            grid={"magic_parameter": [1]},
        )
