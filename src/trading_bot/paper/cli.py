import argparse

from trading_bot.data import HistoricalDataStore, validate_ohlcv
from trading_bot.paper import (
    PaperBroker,
    PaperJournal,
    compare_paper_to_backtest,
    run_paper_replay,
)


def _add_dataset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--market-type", default="spot")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--root", default="data/history")
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--strategies",
        default="trend_continuation",
        help="Comma-separated strategy candidates for replay/comparison.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run and inspect read-only paper trading simulations."
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    replay = subparsers.add_parser("replay")
    _add_dataset_args(replay)
    replay.add_argument(
        "--journal",
        default="data/paper/paper_trading.db",
    )

    compare = subparsers.add_parser("compare")
    _add_dataset_args(compare)

    status = subparsers.add_parser("status")
    status.add_argument(
        "--journal",
        default="data/paper/paper_trading.db",
    )
    status.add_argument("--limit", type=int, default=20)

    return parser


def _validate_risk(args) -> None:
    if not 0 < args.risk_percent <= 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")


def _strategy_names(args) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in args.strategies.split(",")
        if value.strip()
    )


def _load_data(args):
    store = HistoricalDataStore(args.root)
    data = store.load(
        args.exchange,
        args.market_type,
        args.symbol,
        args.timeframe,
    )

    quality = validate_ohlcv(
        data,
        timeframe=args.timeframe,
        min_rows=220,
    )
    if not quality.valid:
        raise SystemExit(
            "Historical dataset is not usable: "
            + "; ".join(quality.issues)
        )

    return data


def _replay(args) -> int:
    _validate_risk(args)
    data = _load_data(args)

    journal = PaperJournal(args.journal)
    broker = PaperBroker(
        starting_equity=args.equity,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        journal=journal,
    )

    run_paper_replay(
        data=data,
        symbol=args.symbol,
        timeframe=args.timeframe,
        broker=broker,
        risk_percent=args.risk_percent / 100,
        min_confirmed_score=args.min_score,
        allow_short=args.market_type != "spot",
        strategy_names=_strategy_names(args),
    )

    snapshot = broker.snapshot()
    print("\n=== PAPER REPLAY ===")
    print(f"Symbol:             {args.symbol}")
    print(f"Timeframe:          {args.timeframe}")
    print(f"Starting equity:    {snapshot.starting_equity:,.2f}")
    print(f"Ending equity:      {snapshot.equity:,.2f}")
    print(f"Realized net P&L:   {snapshot.realized_net_pnl:,.2f}")
    print(f"Fees paid:          {snapshot.fees_paid:,.2f}")
    print(f"Completed trades:   {len(broker.completed_trades)}")

    trades = journal.trades(limit=10)
    if not trades.empty:
        columns = [
            "symbol",
            "signal",
            "strategy",
            "entry_time",
            "exit_time",
            "net_pnl",
            "r_multiple",
            "exit_reason",
            "score",
        ]
        print("\n=== RECENT PAPER TRADES ===")
        print(trades[columns].to_string(index=False))

    journal.close()
    return 0


def _compare(args) -> int:
    _validate_risk(args)
    data = _load_data(args)

    comparison = compare_paper_to_backtest(
        data=data,
        symbol=args.symbol,
        timeframe=args.timeframe,
        initial_equity=args.equity,
        risk_percent=args.risk_percent / 100,
        min_confirmed_score=args.min_score,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        allow_short=args.market_type != "spot",
        strategy_names=_strategy_names(args),
    )

    print("\n=== PAPER VS BACKTEST ===")
    print(
        f"Backtest final equity: {comparison.backtest_final_equity:,.2f}"
    )
    print(
        f"Paper final equity:    {comparison.paper_final_equity:,.2f}"
    )
    print(
        f"Equity delta:          {comparison.final_equity_delta:,.6f}"
    )
    print(
        f"Backtest return:       {comparison.backtest_return_pct:.4f}%"
    )
    print(
        f"Paper return:          {comparison.paper_return_pct:.4f}%"
    )
    print(
        f"Return delta:          {comparison.return_delta_pct:.6f}%"
    )
    print(
        f"Backtest trades:       {comparison.backtest_trades}"
    )
    print(
        f"Paper trades:          {comparison.paper_trades}"
    )
    print(
        f"Trade-count delta:     {comparison.trade_count_delta}"
    )

    return 0


def _status(args) -> int:
    journal = PaperJournal(args.journal)

    orders = journal.orders(limit=args.limit)
    trades = journal.trades(limit=args.limit)

    print("\n=== PAPER ORDERS ===")
    if orders.empty:
        print("No paper orders recorded.")
    else:
        print(
            orders[
                [
                    "order_id",
                    "symbol",
                    "timeframe",
                    "signal",
                    "status",
                    "score",
                    "created_at",
                    "filled_at",
                    "closed_at",
                ]
            ].to_string(index=False)
        )

    print("\n=== PAPER TRADES ===")
    if trades.empty:
        print("No completed paper trades recorded.")
    else:
        print(
            trades[
                [
                    "symbol",
                    "signal",
                    "strategy",
                    "net_pnl",
                    "r_multiple",
                    "exit_reason",
                    "exit_time",
                ]
            ].to_string(index=False)
        )

    journal.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "replay":
        return _replay(args)
    if args.command == "compare":
        return _compare(args)
    if args.command == "status":
        return _status(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
