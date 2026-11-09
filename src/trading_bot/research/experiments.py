from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from itertools import product
import json

import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.planning import TradePlanConfig
from trading_bot.strategies import DEFAULT_STRATEGIES, normalize_strategy_names


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def training_slice(
    data: pd.DataFrame,
    train_end,
    train_start=None,
    min_rows: int = 220,
) -> pd.DataFrame:
    if "timestamp" not in data.columns:
        raise ValueError("Experiment data requires a timestamp column.")

    ordered = data.sort_values("timestamp").reset_index(drop=True).copy()
    ordered["timestamp"] = pd.to_datetime(
        ordered["timestamp"],
        utc=True,
    )
    end = _utc_timestamp(train_end)
    start = (
        _utc_timestamp(train_start)
        if train_start is not None
        else None
    )
    if start is not None and end <= start:
        raise ValueError("train_end must be after train_start.")

    mask = ordered["timestamp"] < end
    if start is not None:
        mask &= ordered["timestamp"] >= start

    training = ordered.loc[mask].reset_index(drop=True)
    if len(training) < min_rows:
        raise ValueError(
            f"Training slice requires at least {min_rows} rows; "
            f"received {len(training)}."
        )
    return training


def expand_parameter_grid(
    grid: dict[str, list],
    max_combinations: int = 500,
) -> list[dict]:
    if not grid:
        raise ValueError("Parameter grid cannot be empty.")
    if max_combinations <= 0:
        raise ValueError("max_combinations must be positive.")

    keys = sorted(grid)
    values = []
    combinations = 1
    for key in keys:
        options = grid[key]
        if not isinstance(options, list) or not options:
            raise ValueError(
                f"Grid parameter {key} must contain a non-empty list."
            )
        combinations *= len(options)
        if combinations > max_combinations:
            raise ValueError(
                f"Parameter grid expands to {combinations} combinations; "
                f"limit is {max_combinations}."
            )
        values.append(options)

    return [
        dict(zip(keys, combination))
        for combination in product(*values)
    ]


def _set_parameter(
    backtest_kwargs: dict,
    strategy_parameters: dict[str, dict],
    planner_values: dict,
    path: str,
    value,
) -> None:
    if path in {
        "min_confirmed_score",
        "entry_valid_bars",
        "decision_lookback",
    }:
        backtest_kwargs[path] = value
        return

    if path.startswith("planner."):
        name = path.split(".", 1)[1]
        if name not in planner_values:
            raise ValueError(
                f"Unknown trade-plan parameter: {name}"
            )
        planner_values[name] = value
        return

    if path.startswith("strategy."):
        parts = path.split(".", 2)
        if len(parts) != 3:
            raise ValueError(
                "Strategy parameter paths must use "
                "strategy.<name>.<parameter>."
            )
        _, strategy, name = parts
        strategy_parameters.setdefault(strategy, {})[name] = value
        return

    raise ValueError(
        "Unsupported parameter path: "
        f"{path}. Use min_confirmed_score, entry_valid_bars, "
        "decision_lookback, planner.<parameter>, or "
        "strategy.<name>.<parameter>."
    )


def _experiment_id(
    strategy_names: tuple[str, ...],
    parameters: dict,
) -> str:
    canonical = json.dumps(
        {
            "strategies": strategy_names,
            "parameters": parameters,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _average_r(trades: pd.DataFrame) -> float:
    if trades.empty or "r_multiple" not in trades.columns:
        return 0.0
    return float(trades["r_multiple"].mean())


def run_parameter_experiments(
    data: pd.DataFrame,
    grid: dict[str, list],
    train_end,
    train_start=None,
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    allow_short: bool = True,
    base_min_confirmed_score: int = 70,
    base_entry_valid_bars: int = 3,
    base_decision_lookback: int = 250,
    base_strategy_parameters: dict[str, dict] | None = None,
    base_trade_plan_config: TradePlanConfig | None = None,
    max_combinations: int = 500,
) -> pd.DataFrame:
    """Evaluate a declared parameter grid on TRAIN data only.

    This function intentionally does not rank or promote a winner. The output
    is an auditable experiment table for later validation on untouched data.
    """
    strategies = normalize_strategy_names(strategy_names)
    training = training_slice(
        data,
        train_end=train_end,
        train_start=train_start,
    )
    combinations = expand_parameter_grid(
        grid,
        max_combinations=max_combinations,
    )

    rows: list[dict] = []
    base_plan = asdict(
        base_trade_plan_config or TradePlanConfig()
    )

    for parameters in combinations:
        backtest_kwargs = {
            "initial_equity": initial_equity,
            "risk_percent": risk_percent,
            "min_confirmed_score": base_min_confirmed_score,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "entry_valid_bars": base_entry_valid_bars,
            "decision_lookback": base_decision_lookback,
            "allow_short": allow_short,
            "strategy_names": strategies,
        }
        strategy_parameters = deepcopy(
            base_strategy_parameters or {}
        )
        planner_values = deepcopy(base_plan)

        for path, value in parameters.items():
            _set_parameter(
                backtest_kwargs,
                strategy_parameters,
                planner_values,
                path,
                value,
            )

        trade_plan_config = TradePlanConfig(**planner_values)
        backtest_kwargs["strategy_parameters"] = (
            strategy_parameters
        )
        backtest_kwargs["trade_plan_config"] = (
            trade_plan_config
        )

        result = run_decision_backtest(
            data=training,
            **backtest_kwargs,
        )
        strategy_mix = ""
        if (
            not result.trades.empty
            and "strategy" in result.trades.columns
        ):
            counts = result.trades["strategy"].value_counts()
            strategy_mix = ",".join(
                f"{name}:{int(count)}"
                for name, count in counts.items()
            )

        experiment_id = _experiment_id(
            strategies,
            parameters,
        )
        rows.append(
            {
                "experiment_id": experiment_id,
                "strategies": ",".join(strategies),
                "parameters_json": json.dumps(
                    parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "train_start": training["timestamp"].iloc[0],
                "train_end_exclusive": _utc_timestamp(train_end),
                "training_rows": len(training),
                "trades": result.total_trades,
                "return_pct": result.total_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "win_rate_pct": result.win_rate_pct,
                "profit_factor": result.profit_factor,
                "average_pnl": result.average_pnl,
                "average_r": _average_r(result.trades),
                "strategy_mix": strategy_mix,
            }
        )

    return pd.DataFrame(rows).sort_values(
        "experiment_id"
    ).reset_index(drop=True)
