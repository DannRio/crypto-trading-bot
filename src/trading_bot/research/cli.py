import argparse
from pathlib import Path

import pandas as pd

from trading_bot.data import HistoricalDataStore, download_history
from trading_bot.research.batch import run_batch_research
from trading_bot.research.evaluation import (
    run_out_of_sample_evaluation,
    run_walk_forward_evaluation,
)
from trading_bot.research.models import DatasetSpec


def _csv_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def _add_dataset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--market-type", default="spot")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--timeframes", default="1h")
    parser.add_argument("--root", default="data/history")


def _add_backtest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=5.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download historical candles and run scanner-aligned "
            "strategy research."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download")
    _add_dataset_args(download)
    download.add_argument("--start", required=True)
    download.add_argument("--end", required=True)
    download.add_argument("--batch-limit", type=int, default=1000)

    backtest = subparsers.add_parser("backtest")
    _add_dataset_args(backtest)
    _add_backtest_args(backtest)
    backtest.add_argument("--export")

    oos = subparsers.add_parser("oos")
    _add_dataset_args(oos)
    _add_backtest_args(oos)
    oos.add_argument("--train-cutoff", required=True)
    oos.add_argument("--validation-cutoff", required=True)
    oos.add_argument("--export")

    walk = subparsers.add_parser("walk-forward")
    _add_dataset_args(walk)
    _add_backtest_args(walk)
    walk.add_argument("--train-bars", type=int, required=True)
    walk.add_argument("--test-bars", type=int, required=True)
    walk.add_argument("--step-bars", type=int)
    walk.add_argument("--export")

    return parser


def _validate_risk(args) -> None:
    if not 0 < args.risk_percent <= 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")


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
    }


def _export(frame: pd.DataFrame, destination: str | None) -> None:
    if not destination:
        return

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    print(f"Exported results to {path}")


def _download(args) -> int:
    store = HistoricalDataStore(args.root)

    for symbol in _csv_values(args.symbols):
        for timeframe in _csv_values(args.timeframes):
            data = download_history(
                symbol=symbol,
                start=args.start,
                end=args.end,
                timeframe=timeframe,
                exchange_id=args.exchange,
                market_type=args.market_type,
                batch_limit=args.batch_limit,
                store=store,
            )
            path = store.dataset_path(
                args.exchange,
                args.market_type,
                symbol,
                timeframe,
            )
            print(
                f"{symbol} {timeframe}: "
                f"{len(data):,} fetched -> {path}"
            )

    return 0


def _backtest(args) -> int:
    _validate_risk(args)

    summary, _ = run_batch_research(
        specs=_specs(args),
        store=HistoricalDataStore(args.root),
        **_backtest_kwargs(args),
    )

    if summary.empty:
        print("No research results.")
        return 0

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
        "status",
    ]
    available = [
        column
        for column in display_columns
        if column in summary.columns
    ]
    print(summary[available].to_string(index=False))
    _export(summary, args.export)
    return 0


def _oos(args) -> int:
    _validate_risk(args)
    store = HistoricalDataStore(args.root)
    frames: list[pd.DataFrame] = []

    for spec in _specs(args):
        data = store.load(
            spec.exchange_id,
            spec.market_type,
            spec.symbol,
            spec.timeframe,
        )
        if data.empty:
            print(
                f"Missing dataset: {spec.symbol} {spec.timeframe}"
            )
            continue

        summary, _ = run_out_of_sample_evaluation(
            data=data,
            train_cutoff=args.train_cutoff,
            validation_cutoff=args.validation_cutoff,
            **_backtest_kwargs(args),
        )
        summary.insert(0, "timeframe", spec.timeframe)
        summary.insert(0, "symbol", spec.symbol)
        summary.insert(0, "market_type", spec.market_type)
        summary.insert(0, "exchange", spec.exchange_id)
        frames.append(summary)

    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame()
    )
    if combined.empty:
        print("No out-of-sample results.")
        return 0

    print(combined.to_string(index=False))
    _export(combined, args.export)
    return 0


def _walk_forward(args) -> int:
    _validate_risk(args)
    store = HistoricalDataStore(args.root)
    frames: list[pd.DataFrame] = []

    for spec in _specs(args):
        data = store.load(
            spec.exchange_id,
            spec.market_type,
            spec.symbol,
            spec.timeframe,
        )
        if data.empty:
            print(
                f"Missing dataset: {spec.symbol} {spec.timeframe}"
            )
            continue

        summary, _ = run_walk_forward_evaluation(
            data=data,
            train_bars=args.train_bars,
            test_bars=args.test_bars,
            step_bars=args.step_bars,
            **_backtest_kwargs(args),
        )
        summary.insert(0, "timeframe", spec.timeframe)
        summary.insert(0, "symbol", spec.symbol)
        summary.insert(0, "market_type", spec.market_type)
        summary.insert(0, "exchange", spec.exchange_id)
        frames.append(summary)

    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame()
    )
    if combined.empty:
        print("No walk-forward windows were produced.")
        return 0

    print(combined.to_string(index=False))
    _export(combined, args.export)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "download":
        return _download(args)
    if args.command == "backtest":
        return _backtest(args)
    if args.command == "oos":
        return _oos(args)
    if args.command == "walk-forward":
        return _walk_forward(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
