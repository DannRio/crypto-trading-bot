import argparse

import pandas as pd

from trading_bot.data import HistoricalDataStore, validate_ohlcv
from trading_bot.paper import (
    PaperBroker,
    PaperJournal,
    PaperServiceConfig,
    PaperTradingService,
    PortfolioRiskConfig,
    PortfolioRiskManager,
    compare_paper_to_backtest,
    run_paper_replay,
)


def _csv_values(raw: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in raw.split(",")
        if value.strip()
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


def _add_daemon_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--market-type", default="spot")
    parser.add_argument(
        "--symbols",
        default="BTC/USDT,ETH/USDT,SOL/USDT",
        help="Comma-separated symbols used when auto-universe is disabled.",
    )
    parser.add_argument("--auto-universe", action="store_true")
    parser.add_argument(
        "--quote-currencies",
        default="USDT",
    )
    parser.add_argument("--max-markets", type=int, default=20)
    parser.add_argument(
        "--min-quote-volume",
        type=float,
        default=1_000_000,
    )
    parser.add_argument("--max-spread-bps", type=float)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument(
        "--confirm-timeframes",
        default="",
        help="Comma-separated higher timeframe regime gates.",
    )
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument(
        "--strategies",
        default="trend_continuation",
    )
    parser.add_argument("--fee-rate", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--entry-expiry-bars",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
    )
    parser.add_argument(
        "--max-open-risk-percent",
        type=float,
        default=3.0,
    )
    parser.add_argument(
        "--max-symbol-risk-percent",
        type=float,
        default=1.5,
    )
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument(
        "--max-pending-orders",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--max-gross-notional-percent",
        type=float,
        default=200.0,
    )
    parser.add_argument(
        "--max-correlated-risk-percent",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--correlation-threshold",
        type=float,
        default=0.75,
    )
    parser.add_argument(
        "--allow-missing-correlation-data",
        action="store_true",
    )
    parser.add_argument(
        "--max-daily-loss-percent",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--max-weekly-loss-percent",
        type=float,
        default=5.0,
    )
    parser.add_argument(
        "--max-drawdown-percent",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--correlation-lookback",
        type=int,
        default=120,
    )
    parser.add_argument(
        "--min-correlation-samples",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--journal",
        default="data/paper/continuous_paper.db",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one service cycle and exit.",
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

    daemon = subparsers.add_parser("daemon")
    _add_daemon_args(daemon)

    status = subparsers.add_parser("status")
    status.add_argument(
        "--journal",
        default="data/paper/continuous_paper.db",
    )
    status.add_argument("--limit", type=int, default=20)

    risk_status = subparsers.add_parser("risk-status")
    risk_status.add_argument(
        "--journal",
        default="data/paper/continuous_paper.db",
    )

    risk_reset = subparsers.add_parser("risk-reset")
    risk_reset.add_argument(
        "--journal",
        default="data/paper/continuous_paper.db",
    )
    risk_reset.add_argument(
        "--confirm",
        required=True,
        help="Must be exactly RESET.",
    )
    risk_reset.add_argument(
        "--max-state-age-minutes",
        type=float,
        default=10.0,
    )

    return parser


def _validate_risk(args) -> None:
    if not 0 < args.risk_percent <= 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")


def _strategy_names(args) -> tuple[str, ...]:
    return _csv_values(args.strategies)




def _portfolio_risk_config(args) -> PortfolioRiskConfig:
    return PortfolioRiskConfig(
        max_open_risk_percent=(
            args.max_open_risk_percent / 100
        ),
        max_positions=args.max_positions,
        max_pending_orders=args.max_pending_orders,
        max_symbol_risk_percent=(
            args.max_symbol_risk_percent / 100
        ),
        max_gross_notional_percent=(
            args.max_gross_notional_percent / 100
        ),
        max_correlated_risk_percent=(
            args.max_correlated_risk_percent / 100
        ),
        correlation_threshold=args.correlation_threshold,
        require_correlation_data=(
            not args.allow_missing_correlation_data
        ),
        max_daily_loss_percent=(
            args.max_daily_loss_percent / 100
        ),
        max_weekly_loss_percent=(
            args.max_weekly_loss_percent / 100
        ),
        max_drawdown_percent=(
            args.max_drawdown_percent / 100
        ),
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


def _print_service_report(report) -> None:
    snapshot = report.snapshot
    print(
        "Paper cycle | "
        f"scanned={report.scanned_markets} "
        f"managed={report.managed_markets} "
        f"new_candles={report.new_candles} "
        f"submitted={report.submitted_orders} "
        f"rejected={report.rejected_signals} "
        f"duplicates={report.duplicate_signals} "
        f"closed={report.closed_trades} "
        f"scan_errors={report.scan_errors} "
        f"management_errors={report.management_errors} "
        f"risk_halted={report.risk_halted} "
        f"daily_loss={report.daily_loss_pct:.2%} "
        f"weekly_loss={report.weekly_loss_pct:.2%} "
        f"drawdown={report.drawdown_pct:.2%} "
        f"equity={snapshot.equity:,.2f} "
        f"open={snapshot.open_positions} "
        f"pending={snapshot.pending_orders}"
    )


def _daemon(args) -> int:
    _validate_risk(args)

    config = PaperServiceConfig(
        exchange_id=args.exchange,
        market_type=args.market_type,
        timeframe=args.timeframe,
        confirmation_timeframes=_csv_values(
            args.confirm_timeframes
        ),
        symbols=_csv_values(args.symbols),
        auto_universe=args.auto_universe,
        quote_currencies=tuple(
            value.upper()
            for value in _csv_values(
                args.quote_currencies
            )
        ),
        max_markets=args.max_markets,
        min_quote_volume=args.min_quote_volume,
        max_spread_bps=args.max_spread_bps,
        starting_equity=args.equity,
        risk_percent=args.risk_percent / 100,
        min_confirmed_score=args.min_score,
        strategy_names=_strategy_names(args),
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        entry_expiry_bars=args.entry_expiry_bars,
        interval_seconds=args.interval_seconds,
        correlation_lookback=args.correlation_lookback,
        min_correlation_samples=args.min_correlation_samples,
        portfolio_risk=_portfolio_risk_config(args),
    )

    journal = PaperJournal(args.journal)
    try:
        service = PaperTradingService(
            config=config,
            journal=journal,
        )
        restored = service.broker.snapshot()
        print(
            "Paper service restored | "
            f"equity={restored.equity:,.2f} "
            f"open={restored.open_positions} "
            f"pending={restored.pending_orders}"
        )

        if args.once:
            _print_service_report(service.run_once())
            return 0

        def on_error(exc: Exception) -> None:
            print(
                f"Paper service cycle error: "
                f"{type(exc).__name__}: {exc}"
            )

        try:
            service.run_forever(
                on_cycle=_print_service_report,
                on_error=on_error,
            )
        except KeyboardInterrupt:
            print(
                "Paper service stopped; persisted paper "
                "orders and positions were preserved."
            )
        return 0
    finally:
        journal.close()


def _status(args) -> int:
    journal = PaperJournal(args.journal)

    orders = journal.orders(limit=args.limit)
    trades = journal.trades(limit=args.limit)
    checkpoints = journal.checkpoints()
    signals = journal.signal_events(limit=args.limit)
    risk_state = journal.load_risk_state()

    print("\n=== PORTFOLIO RISK ===")
    if risk_state is None:
        print("No portfolio risk state recorded.")
    else:
        print(
            f"Halted: {risk_state.halted} | "
            f"reason: {risk_state.halt_reason or 'none'} | "
            f"last equity: {risk_state.last_equity:,.2f} | "
            f"peak: {risk_state.peak_equity:,.2f} | "
            f"evaluated: {risk_state.evaluated_at}"
        )

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
                    "strategy",
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

    print("\n=== SERVICE CHECKPOINTS ===")
    if checkpoints.empty:
        print("No continuous-service checkpoints recorded.")
    else:
        print(checkpoints.to_string(index=False))

    print("\n=== RECENT SIGNAL EVENTS ===")
    if signals.empty:
        print("No continuous-service signal events recorded.")
    else:
        print(signals.to_string(index=False))

    journal.close()
    return 0




def _risk_status(args) -> int:
    journal = PaperJournal(args.journal)
    try:
        state = journal.load_risk_state()
        if state is None:
            print("No portfolio risk state recorded.")
            return 0

        print("\n=== PORTFOLIO RISK STATE ===")
        print(f"Halted:             {state.halted}")
        print(
            f"Halt reason:         "
            f"{state.halt_reason or 'none'}"
        )
        print(f"Last equity:         {state.last_equity:,.2f}")
        print(f"Peak equity:         {state.peak_equity:,.2f}")
        print(f"Day start equity:    {state.day_start_equity:,.2f}")
        print(f"Week start equity:   {state.week_start_equity:,.2f}")
        print(f"Evaluated at:        {state.evaluated_at}")
        print(f"Halted at:           {state.halted_at or 'n/a'}")
        return 0
    finally:
        journal.close()


def _risk_reset(args) -> int:
    if args.confirm != "RESET":
        raise SystemExit("--confirm must be exactly RESET.")
    if args.max_state_age_minutes <= 0:
        raise SystemExit(
            "--max-state-age-minutes must be positive."
        )

    journal = PaperJournal(args.journal)
    try:
        state = journal.load_risk_state()
        if state is None:
            raise SystemExit(
                "No portfolio risk state exists to reset."
            )
        if not state.halted:
            raise SystemExit(
                "Portfolio risk halt is not active; nothing to reset."
            )

        now = pd.Timestamp.now(tz="UTC")
        evaluated = pd.Timestamp(state.evaluated_at)
        if evaluated.tzinfo is None:
            evaluated = evaluated.tz_localize("UTC")
        else:
            evaluated = evaluated.tz_convert("UTC")
        age_minutes = (
            (now - evaluated).total_seconds() / 60
        )
        if age_minutes > args.max_state_age_minutes:
            raise SystemExit(
                "Risk state is stale "
                f"({age_minutes:.1f} minutes old). "
                "Run the paper daemon once to refresh marks "
                "before resetting the halt."
            )

        manager = PortfolioRiskManager()
        reset = manager.reset_state(
            account_equity=state.last_equity,
            timestamp=now,
        )
        journal.save_risk_state(reset)
        print(
            "Portfolio risk halt reset at "
            f"equity {reset.last_equity:,.2f}."
        )
        return 0
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "replay":
        return _replay(args)
    if args.command == "compare":
        return _compare(args)
    if args.command == "daemon":
        return _daemon(args)
    if args.command == "status":
        return _status(args)
    if args.command == "risk-status":
        return _risk_status(args)
    if args.command == "risk-reset":
        return _risk_reset(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
