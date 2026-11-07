import argparse
from pathlib import Path

from trading_bot.data import HistoricalDataStore, download_history
from trading_bot.research.batch import run_batch_research
from trading_bot.research.models import DatasetSpec


def _csv_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download historical candles and run batch strategy research."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download")
    download.add_argument("--exchange", required=True)
    download.add_argument("--market-type", default="spot")
    download.add_argument("--symbols", required=True)
    download.add_argument("--timeframes", default="1h")
    download.add_argument("--start", required=True)
    download.add_argument("--end", required=True)
    download.add_argument("--batch-limit", type=int, default=1000)
    download.add_argument("--root", default="data/history")

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--exchange", required=True)
    backtest.add_argument("--market-type", default="spot")
    backtest.add_argument("--symbols", required=True)
    backtest.add_argument("--timeframes", default="1h")
    backtest.add_argument("--equity", type=float, default=10_000)
    backtest.add_argument("--risk-percent", type=float, default=1.0)
    backtest.add_argument("--min-score", type=int, default=70)
    backtest.add_argument("--fee-rate", type=float, default=0.001)
    backtest.add_argument("--slippage-bps", type=float, default=5.0)
    backtest.add_argument("--root", default="data/history")
    backtest.add_argument("--export")

    return parser


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
    if not 0 < args.risk_percent <= 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")

    specs = [
        DatasetSpec(
            exchange_id=args.exchange,
            market_type=args.market_type,
            symbol=symbol,
            timeframe=timeframe,
        )
        for symbol in _csv_values(args.symbols)
        for timeframe in _csv_values(args.timeframes)
    ]

    summary, _ = run_batch_research(
        specs=specs,
        store=HistoricalDataStore(args.root),
        initial_equity=args.equity,
        risk_percent=args.risk_percent / 100,
        min_confirmed_score=args.min_score,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
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

    if args.export:
        path = Path(args.export)
        path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(path, index=False)
        print(f"Exported summary to {path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "download":
        return _download(args)
    if args.command == "backtest":
        return _backtest(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
