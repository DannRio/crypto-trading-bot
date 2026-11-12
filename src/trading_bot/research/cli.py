import argparse
import json
import math
from pathlib import Path

import pandas as pd

from trading_bot.data import HistoricalDataStore, download_history, timeframe_to_milliseconds, validate_ohlcv
from trading_bot.research.batch import (
    ResearchDataError, dataset_identity, load_research_inputs, run_batch_research,
)
from trading_bot.research.evaluation import (
    run_out_of_sample_evaluation,
    run_walk_forward_evaluation,
)
from trading_bot.research.experiments import run_parameter_experiments
from trading_bot.research.models import DatasetSpec
from trading_bot.research.universe import (
    SELECTION_WARNING, load_universe_snapshot, save_universe_snapshot,
)
from trading_bot.scanner import (
    DynamicMarketUniverse, UniverseConfig,
    validate_confirmation_timeframes, validate_entry_timeframe,
)


def _csv_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def _add_dataset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", help="Required for manual symbols; inferred from a frozen universe.")
    parser.add_argument("--market-type", choices=("spot", "swap", "future"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--symbols", help="Comma-separated exact CCXT symbols.")
    source.add_argument("--universe-file", help="Frozen universe JSON; never rediscovers assets during research.")
    parser.add_argument("--timeframes", default="1h")
    parser.add_argument("--root", default="data/history")


def _add_backtest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--strategies",
        default="trend_continuation_v2",
        help=(
            "Comma-separated strategies: trend_continuation, "
            "trend_continuation_v2, breakout_retest, range_mean_reversion."
        ),
    )


def _add_timeframe_gates(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm-timeframes", default="", help="Saved higher-timeframe context, e.g. 4h.")
    parser.add_argument("--entry-timeframe", default="", help="Saved lower-timeframe entry confirmation, e.g. 15m.")
    parser.add_argument("--execution-timeframe", default="", help="Saved lower-timeframe execution candles, e.g. 15m.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download historical candles and run scanner-aligned "
            "strategy research."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    universe = subparsers.add_parser("universe", help="Discover and freeze a current liquid-market selection.")
    universe.add_argument("--exchange", required=True)
    universe.add_argument("--market-type", default="spot", choices=("spot", "swap", "future"))
    universe.add_argument("--quote-currencies", default="USDT")
    universe.add_argument("--max-markets", type=int, default=30)
    universe.add_argument("--min-quote-volume", type=float, default=1_000_000)
    universe.add_argument("--max-spread-bps", type=float)
    universe.add_argument("--min-history-candles", type=int, default=220)
    universe.add_argument("--history-timeframe", default="1h")
    universe.add_argument("--max-history-probes", type=int, default=90)
    universe.add_argument("--max-ticker-requests", type=int, default=200)
    universe.add_argument("--allow-duplicate-bases", action="store_true")
    universe.add_argument("--output", required=True, help="New JSON file; existing snapshots cannot be overwritten.")

    download = subparsers.add_parser("download")
    _add_dataset_args(download)
    download.add_argument("--start", required=True)
    download.add_argument("--end", required=True)
    download.add_argument("--batch-limit", type=int, default=1000)
    download.add_argument("--export", help="Export per-dataset download coverage and errors.")

    backtest = subparsers.add_parser("backtest")
    _add_dataset_args(backtest)
    _add_backtest_args(backtest)
    _add_timeframe_gates(backtest)
    backtest.add_argument("--export")

    oos = subparsers.add_parser("oos")
    _add_dataset_args(oos)
    _add_backtest_args(oos)
    _add_timeframe_gates(oos)
    oos.add_argument("--train-cutoff", required=True)
    oos.add_argument("--validation-cutoff", required=True)
    oos.add_argument("--export")

    walk = subparsers.add_parser("walk-forward")
    _add_dataset_args(walk)
    _add_backtest_args(walk)
    _add_timeframe_gates(walk)
    walk.add_argument("--train-bars", type=int, required=True)
    walk.add_argument("--test-bars", type=int, required=True)
    walk.add_argument("--step-bars", type=int)
    walk.add_argument("--export")

    experiment = subparsers.add_parser("experiment")
    _add_dataset_args(experiment)
    _add_backtest_args(experiment)
    experiment.add_argument("--train-start")
    experiment.add_argument("--train-end", required=True)
    experiment.add_argument(
        "--grid",
        required=True,
        help="Path to a JSON parameter grid.",
    )
    experiment.add_argument(
        "--max-combinations",
        type=int,
        default=500,
    )
    experiment.add_argument("--export")

    return parser


def _validate_risk(args) -> None:
    if not math.isfinite(args.risk_percent) or not 0 < args.risk_percent <= 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")
    if not math.isfinite(args.equity) or args.equity <= 0:
        raise SystemExit("--equity must be positive and finite.")
    if not math.isfinite(args.fee_rate) or not 0 <= args.fee_rate < 1:
        raise SystemExit("--fee-rate must be finite and between 0 (inclusive) and 1.")
    if not math.isfinite(args.slippage_bps) or not 0 <= args.slippage_bps < 10_000:
        raise SystemExit("--slippage-bps must be finite and between 0 (inclusive) and 10000.")
    if not 0 <= args.min_score <= 100:
        raise SystemExit("--min-score must be between 0 and 100.")


def _resolve_dataset_source(args) -> None:
    if args.universe_file:
        try:
            snapshot = load_universe_snapshot(args.universe_file)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Could not load frozen universe: {exc}") from exc
        if args.exchange and args.exchange != snapshot["exchange_id"]:
            raise SystemExit("--exchange must match the frozen universe exchange.")
        if args.market_type and args.market_type != snapshot["market_type"]:
            raise SystemExit("--market-type must match the frozen universe market type.")
        args.exchange = snapshot["exchange_id"]
        args.market_type = snapshot["market_type"]
        args.symbols = ",".join(snapshot["symbols"])
        args.universe_snapshot = snapshot
        print(f"Frozen universe: {len(snapshot['symbols'])} markets; selected {snapshot['selected_at_utc']}; sha256={snapshot['snapshot_sha256']}")
        print(SELECTION_WARNING)
    else:
        if not args.exchange:
            raise SystemExit("--exchange is required with --symbols.")
        args.market_type = args.market_type or "spot"
        if not _csv_values(args.symbols):
            raise SystemExit("At least one nonempty symbol is required.")
        symbols = _csv_values(args.symbols)
        if len(symbols) != len(set(symbols)):
            raise SystemExit("Duplicate symbols are not allowed.")
    if not _csv_values(args.timeframes):
        raise SystemExit("At least one timeframe is required.")
    if len(_csv_values(args.timeframes)) != len(set(_csv_values(args.timeframes))):
        raise SystemExit("Duplicate timeframes are not allowed.")
    try:
        for timeframe in _csv_values(args.timeframes):
            duration = timeframe_to_milliseconds(timeframe)
            if duration <= 0:
                raise ValueError("Timeframes must be positive.")
            if hasattr(args, "confirm_timeframes"):
                validate_confirmation_timeframes(timeframe, tuple(_csv_values(args.confirm_timeframes)))
                validate_entry_timeframe(timeframe, args.entry_timeframe.strip() or None)
                if args.execution_timeframe:
                    execution = timeframe_to_milliseconds(args.execution_timeframe)
                    if not 0 < execution < duration or duration % execution:
                        raise ValueError("Execution candles must be lower than and divide every primary timeframe exactly.")
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"Invalid research timeframes: {exc}") from exc


def _universe(args) -> int:
    destination = Path(args.output)
    if destination.exists():
        raise SystemExit("Universe output already exists; choose a new file to preserve the frozen selection.")
    try:
        config = UniverseConfig(
            market_type=args.market_type,
            quote_currencies=tuple(value.upper() for value in _csv_values(args.quote_currencies)),
            max_markets=args.max_markets, min_quote_volume=args.min_quote_volume,
            max_spread_bps=args.max_spread_bps, min_history_candles=args.min_history_candles,
            history_timeframe=args.history_timeframe, max_history_probes=args.max_history_probes,
            max_ticker_requests=args.max_ticker_requests, unique_base_assets=not args.allow_duplicate_bases,
        )
        selection = DynamicMarketUniverse(args.exchange, args.market_type).select(config)
        snapshot = save_universe_snapshot(destination, args.exchange, config, selection)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"Could not freeze universe: {exc}") from exc
    print(f"Frozen {len(selection.symbols)} markets in {destination}: {','.join(selection.symbols)}")
    print(f"Snapshot SHA256: {snapshot['snapshot_sha256']}")
    print(SELECTION_WARNING)
    for warning in selection.warnings:
        print(f"Universe warning: {warning}")
    return 0


def _specs(args) -> list[DatasetSpec]:
    return [
        DatasetSpec(
            exchange_id=args.exchange,
            market_type=args.market_type,
            symbol=symbol,
            timeframe=timeframe,
        )
        for symbol in _csv_values(args.symbols)
        for timeframe in _csv_values(args.timeframes)
    ]


def _backtest_kwargs(args) -> dict:
    return {
        "initial_equity": args.equity,
        "risk_percent": args.risk_percent / 100,
        "min_confirmed_score": args.min_score,
        "fee_rate": args.fee_rate,
        "slippage_bps": args.slippage_bps,
        "allow_short": args.market_type != "spot",
        "strategy_names": tuple(_csv_values(args.strategies)),
    }


def _timeframe_kwargs(args) -> dict:
    return {"confirmation_timeframes": tuple(_csv_values(args.confirm_timeframes)),
            "entry_timeframe": args.entry_timeframe.strip() or None,
            "execution_timeframe": args.execution_timeframe.strip() or None}


def _export(frame: pd.DataFrame, destination: str | None, args=None) -> None:
    if not destination:
        return

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    print(f"Exported results to {path}")
    if args is not None:
        manifest = {"command": args.command,
                    "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
                    "arguments": {key: value for key, value in vars(args).items() if key != "universe_snapshot"},
                    "universe_snapshot": getattr(args, "universe_snapshot", None),
                    "interpretation": "Each asset is a separate simulation. These rows are not a shared-capital portfolio backtest."}
        path.with_suffix(path.suffix + ".manifest.json").write_text(
            json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _failure_exit(frame: pd.DataFrame) -> int:
    return int(frame.empty or ("status" in frame and (frame["status"] != "OK").any()))


def _download(args) -> int:
    store = HistoricalDataStore(args.root)
    rows = []
    try:
        start, end = pd.to_datetime(args.start, utc=True), pd.to_datetime(args.end, utc=True)
        if pd.isna(start) or pd.isna(end):
            raise ValueError("Missing date.")
    except (ValueError, TypeError) as exc:
        raise SystemExit("--start and --end must be valid timestamps.") from exc
    if end <= start:
        raise SystemExit("--end must be later than --start.")
    if args.batch_limit <= 0:
        raise SystemExit("--batch-limit must be positive.")
    for spec in _specs(args):
        row = dict(dataset_identity(spec), rows=0)
        try:
            data = download_history(
                symbol=spec.symbol,
                start=args.start,
                end=args.end,
                timeframe=spec.timeframe,
                exchange_id=args.exchange,
                market_type=args.market_type,
                batch_limit=args.batch_limit,
                store=store,
            )
            quality = validate_ohlcv(data, timeframe=spec.timeframe, min_rows=1)
            row["rows"] = len(data)
            if not quality.valid or quality.missing_intervals:
                row.update(status="INVALID_DATA", error="; ".join(quality.issues + quality.warnings))
            else:
                delta = pd.to_timedelta(timeframe_to_milliseconds(spec.timeframe), unit="ms")
                row.update(start=data.timestamp.min(), end=data.timestamp.max(), status="OK", error="")
                if data.timestamp.min() > start or data.timestamp.max() + delta < end:
                    row.update(status="PARTIAL_DATA", error="Available history does not cover the full requested date range.")
        except Exception as exc:
            # Exchange errors are isolated so every requested dataset is accounted for.
            row.update(status="DOWNLOAD_ERROR", error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
        print(f"{spec.symbol} {spec.timeframe}: {row['status']} ({row['rows']:,} candles) {row.get('error', '')}")
    summary = pd.DataFrame(rows)
    _export(summary, args.export, args)
    return _failure_exit(summary)


def _backtest(args) -> int:
    _validate_risk(args)

    options = _backtest_kwargs(args)
    # Market type belongs to each DatasetSpec; the batch engine applies its own
    # spot-long-only policy rather than accepting a global short override.
    options.pop("allow_short")
    summary, _ = run_batch_research(
        specs=_specs(args),
        store=HistoricalDataStore(args.root),
        **options,
        **_timeframe_kwargs(args),
    )

    if summary.empty:
        print("No research results.")
        return 1

    display_columns = [
        "exchange",
        "market_type",
        "symbol",
        "timeframe",
        "rows",
        "trades",
        "return_pct",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "average_r",
        "strategies",
        "strategy_mix",
        "status",
        "error",
    ]
    available = [
        column
        for column in display_columns
        if column in summary.columns
    ]
    print(summary[available].to_string(index=False))
    _export(summary, args.export, args)
    return _failure_exit(summary)


def _oos(args) -> int:
    return _segmented_research(args, "oos")


def _walk_forward(args) -> int:
    return _segmented_research(args, "walk-forward")


def _segmented_research(args, mode: str) -> int:
    _validate_risk(args)
    store = HistoricalDataStore(args.root)
    frames: list[pd.DataFrame] = []
    gates = _timeframe_kwargs(args)
    for spec in _specs(args):
        try:
            data, secondary = load_research_inputs(spec, store, **gates)
            common = dict(data=data, primary_timeframe=spec.timeframe, secondary_data=secondary,
                          **gates, **_backtest_kwargs(args))
            if mode == "oos":
                summary, _ = run_out_of_sample_evaluation(
                    train_cutoff=args.train_cutoff, validation_cutoff=args.validation_cutoff, **common)
            else:
                summary, _ = run_walk_forward_evaluation(
                    train_bars=args.train_bars, test_bars=args.test_bars,
                    step_bars=args.step_bars, **common)
            if summary.empty:
                summary = pd.DataFrame([{"status": "INSUFFICIENT_DATA", "error": "No complete evaluation windows."}])
            else:
                summary["status"], summary["error"] = "OK", ""
        except ResearchDataError as exc:
            summary = pd.DataFrame([{"status": exc.status, "error": str(exc)}])
        except (ValueError, RuntimeError) as exc:
            summary = pd.DataFrame([{"status": "EVALUATION_ERROR", "error": str(exc)}])
        for key, value in reversed(list(dataset_identity(spec).items())):
            summary.insert(0, key, value)
        summary["strategies"] = args.strategies
        frames.append(summary)
    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame()
    )
    print(combined.to_string(index=False))
    _export(combined, args.export, args)
    return _failure_exit(combined)




def _experiment(args) -> int:
    _validate_risk(args)
    grid_path = Path(args.grid)
    try:
        grid = json.loads(
            grid_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Could not load parameter grid {grid_path}: {exc}"
        ) from exc

    if not isinstance(grid, dict):
        raise SystemExit("Parameter grid JSON must be an object.")

    store = HistoricalDataStore(args.root)
    frames: list[pd.DataFrame] = []

    for spec in _specs(args):
        try:
            data, _ = load_research_inputs(spec, store)
            results = run_parameter_experiments(
                data=data,
                grid=grid,
                train_start=args.train_start,
                train_end=args.train_end,
                strategy_names=tuple(
                    _csv_values(args.strategies)
                ),
                initial_equity=args.equity,
                risk_percent=args.risk_percent / 100,
                fee_rate=args.fee_rate,
                slippage_bps=args.slippage_bps,
                allow_short=args.market_type != "spot",
                base_min_confirmed_score=args.min_score,
                max_combinations=args.max_combinations,
                primary_timeframe=spec.timeframe,
            )
            results["status"], results["error"] = "OK", ""
        except ResearchDataError as exc:
            results = pd.DataFrame([{"status": exc.status, "error": str(exc)}])
        except (ValueError, RuntimeError) as exc:
            results = pd.DataFrame([{"status": "EXPERIMENT_ERROR", "error": str(exc)}])

        results.insert(0, "timeframe", spec.timeframe)
        results.insert(0, "symbol", spec.symbol)
        results.insert(0, "market_type", spec.market_type)
        results.insert(0, "exchange", spec.exchange_id)
        frames.append(results)

    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame()
    )
    if combined.empty:
        print("No parameter experiment results.")
        return 1

    display = [
        "exchange",
        "symbol",
        "timeframe",
        "experiment_id",
        "strategies",
        "parameters_json",
        "trades",
        "return_pct",
        "max_drawdown_pct",
        "profit_factor",
        "average_r",
        "status",
        "error",
    ]
    print(combined[[column for column in display if column in combined]].to_string(index=False))
    _export(combined, args.export, args)
    return _failure_exit(combined)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "universe":
        return _universe(args)
    _resolve_dataset_source(args)
    if args.command == "download":
        return _download(args)
    if args.command == "backtest":
        return _backtest(args)
    if args.command == "oos":
        return _oos(args)
    if args.command == "walk-forward":
        return _walk_forward(args)
    if args.command == "experiment":
        return _experiment(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
