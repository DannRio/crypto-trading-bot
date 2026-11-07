import pandas as pd

from trading_bot.backtesting import BacktestResult, run_decision_backtest


def _average_r(result: BacktestResult) -> float:
    trades = result.trades
    if trades.empty or "r_multiple" not in trades.columns:
        return 0.0
    return float(trades["r_multiple"].mean())


def _segment_backtest(
    data: pd.DataFrame,
    start_index: int,
    end_index: int,
    warmup_candles: int = 220,
    **backtest_kwargs,
) -> BacktestResult:
    if not 0 <= start_index < end_index <= len(data):
        raise ValueError("Invalid segment bounds.")

    history_start = max(0, start_index - warmup_candles)
    segment = data.iloc[history_start:end_index].reset_index(drop=True)

    if start_index == 0:
        local_warmup = warmup_candles
    else:
        local_warmup = start_index - history_start

    return run_decision_backtest(
        data=segment,
        warmup_candles=local_warmup,
        **backtest_kwargs,
    )


def _summary_row(
    name: str,
    data: pd.DataFrame,
    start_index: int,
    end_index: int,
    result: BacktestResult,
) -> dict:
    segment = data.iloc[start_index:end_index]
    return {
        "segment": name,
        "start": segment["timestamp"].iloc[0],
        "end": segment["timestamp"].iloc[-1],
        "rows": len(segment),
        "trades": result.total_trades,
        "return_pct": result.total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "win_rate_pct": result.win_rate_pct,
        "profit_factor": result.profit_factor,
        "average_pnl": result.average_pnl,
        "average_r": _average_r(result),
    }


def run_out_of_sample_evaluation(
    data: pd.DataFrame,
    train_cutoff,
    validation_cutoff,
    warmup_candles: int = 220,
    **backtest_kwargs,
) -> tuple[pd.DataFrame, dict[str, BacktestResult]]:
    """Evaluate fixed strategy parameters over chronological train/val/test periods.

    Cutoffs are exclusive boundaries:
    train < train_cutoff
    validation >= train_cutoff and < validation_cutoff
    test >= validation_cutoff
    """
    ordered = data.sort_values("timestamp").reset_index(drop=True).copy()
    ordered["timestamp"] = pd.to_datetime(
        ordered["timestamp"],
        utc=True,
    )

    train_boundary = pd.Timestamp(train_cutoff)
    validation_boundary = pd.Timestamp(validation_cutoff)
    if train_boundary.tzinfo is None:
        train_boundary = train_boundary.tz_localize("UTC")
    else:
        train_boundary = train_boundary.tz_convert("UTC")
    if validation_boundary.tzinfo is None:
        validation_boundary = validation_boundary.tz_localize("UTC")
    else:
        validation_boundary = validation_boundary.tz_convert("UTC")

    if validation_boundary <= train_boundary:
        raise ValueError("validation_cutoff must be after train_cutoff.")

    train_end = int(
        ordered["timestamp"].searchsorted(train_boundary, side="left")
    )
    validation_end = int(
        ordered["timestamp"].searchsorted(
            validation_boundary,
            side="left",
        )
    )

    boundaries = {
        "TRAIN": (0, train_end),
        "VALIDATION": (train_end, validation_end),
        "TEST": (validation_end, len(ordered)),
    }

    rows: list[dict] = []
    details: dict[str, BacktestResult] = {}

    for name, (start_index, end_index) in boundaries.items():
        if end_index <= start_index:
            raise ValueError(f"{name} period has no candles.")

        result = _segment_backtest(
            ordered,
            start_index,
            end_index,
            warmup_candles=warmup_candles,
            **backtest_kwargs,
        )
        rows.append(
            _summary_row(
                name,
                ordered,
                start_index,
                end_index,
                result,
            )
        )
        details[name] = result

    return pd.DataFrame(rows), details


def run_walk_forward_evaluation(
    data: pd.DataFrame,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
    warmup_candles: int = 220,
    **backtest_kwargs,
) -> tuple[pd.DataFrame, dict[int, dict[str, BacktestResult]]]:
    """Run rolling fixed-parameter train/test windows.

    This is a stability test, not parameter optimization. A later optimizer
    can select parameters on each train window before evaluating its test window.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars and test_bars must be positive.")

    step = step_bars or test_bars
    if step <= 0:
        raise ValueError("step_bars must be positive.")

    ordered = data.sort_values("timestamp").reset_index(drop=True).copy()
    ordered["timestamp"] = pd.to_datetime(
        ordered["timestamp"],
        utc=True,
    )

    rows: list[dict] = []
    details: dict[int, dict[str, BacktestResult]] = {}

    cursor = 0
    window = 1

    while cursor + train_bars + test_bars <= len(ordered):
        train_start = cursor
        train_end = cursor + train_bars
        test_start = train_end
        test_end = test_start + test_bars

        train_result = _segment_backtest(
            ordered,
            train_start,
            train_end,
            warmup_candles=warmup_candles,
            **backtest_kwargs,
        )
        test_result = _segment_backtest(
            ordered,
            test_start,
            test_end,
            warmup_candles=warmup_candles,
            **backtest_kwargs,
        )

        rows.append(
            {
                "window": window,
                "train_start": ordered["timestamp"].iloc[train_start],
                "train_end": ordered["timestamp"].iloc[train_end - 1],
                "test_start": ordered["timestamp"].iloc[test_start],
                "test_end": ordered["timestamp"].iloc[test_end - 1],
                "train_trades": train_result.total_trades,
                "train_return_pct": train_result.total_return_pct,
                "train_profit_factor": train_result.profit_factor,
                "train_average_r": _average_r(train_result),
                "test_trades": test_result.total_trades,
                "test_return_pct": test_result.total_return_pct,
                "test_profit_factor": test_result.profit_factor,
                "test_average_r": _average_r(test_result),
                "test_max_drawdown_pct": test_result.max_drawdown_pct,
            }
        )
        details[window] = {
            "train": train_result,
            "test": test_result,
        }

        cursor += step
        window += 1

    return pd.DataFrame(rows), details
