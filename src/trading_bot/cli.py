import argparse
import os

from trading_bot.alerts import (
    AlertDispatcher,
    AlertLedger,
    ConsoleAlertSink,
    JsonlAlertSink,
    WebhookAlertSink,
    build_scan_alerts,
)
from trading_bot.core.enums import SetupStatus
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)


DEFAULT_SYMBOLS = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,LINK/USDT,ZEC/USDT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan crypto markets for rule-qualified, risk-defined trade setups."
    )
    parser.add_argument("--exchange", default=os.getenv("TRADING_EXCHANGE", "kraken"))
    parser.add_argument("--market-type", default=os.getenv("TRADING_MARKET_TYPE", "spot"))
    parser.add_argument(
        "--symbols",
        default=os.getenv("TRADING_SYMBOLS", DEFAULT_SYMBOLS),
        help="Comma-separated CCXT symbols.",
    )
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument(
        "--confirm-timeframes",
        default="",
        help=(
            "Comma-separated higher timeframes used as regime gates, "
            "for example 4h when the primary timeframe is 1h."
        ),
    )
    parser.add_argument(
        "--entry-timeframe",
        default="",
        help=(
            "Optional lower timeframe used only for entry confirmation, "
            "for example 15m when the primary timeframe is 1h."
        ),
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument(
        "--risk-percent",
        type=float,
        default=1.0,
        help="Account risk per confirmed setup, in percent.",
    )
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument(
        "--strategies",
        default="trend_continuation",
        help=(
            "Comma-separated strategies. Candidates remain opt-in; "
            "trend_continuation is the default."
        ),
    )
    parser.add_argument("--show-no-trade", action="store_true")
    parser.add_argument(
        "--auto-universe",
        action="store_true",
        help="Discover and rank liquid markets instead of using --symbols.",
    )
    parser.add_argument(
        "--quote-currencies",
        default="USDT",
        help="Comma-separated quote currencies for auto-universe mode.",
    )
    parser.add_argument("--max-markets", type=int, default=20)
    parser.add_argument(
        "--min-quote-volume",
        type=float,
        default=1_000_000,
        help="Minimum 24h quote volume for auto-universe markets.",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=None,
        help="Optional maximum ticker bid/ask spread in basis points.",
    )
    parser.add_argument(
        "--alert-sinks",
        default="",
        help=(
            "Comma-separated alert sinks: console,jsonl,webhook. "
            "Empty disables alert delivery."
        ),
    )
    parser.add_argument(
        "--alert-forming",
        action="store_true",
        help="Also alert on FORMING setups.",
    )
    parser.add_argument(
        "--alert-cooldown-minutes",
        type=float,
        default=60.0,
        help="Suppress repeated identical alerts for this many minutes.",
    )
    parser.add_argument(
        "--alert-ledger",
        default="data/alerts/alerts.db",
    )
    parser.add_argument(
        "--alert-jsonl",
        default="data/alerts/events.jsonl",
    )
    parser.add_argument(
        "--alert-webhook-url",
        default=os.getenv("TRADING_ALERT_WEBHOOK_URL", ""),
    )
    return parser


def _quote_currency(symbol: str) -> str:
    if "/" not in symbol:
        return ""
    return symbol.split("/", 1)[1].split(":", 1)[0]


def _print_result(result, show_no_trade: bool) -> None:
    if result.error:
        print(f"[ERROR] {result.symbol}: {result.error}")
        return

    assessment = result.assessment
    if assessment is None:
        print(f"[ERROR] {result.symbol}: no assessment available.")
        return
    if assessment.status == SetupStatus.NO_TRADE and not show_no_trade:
        return

    print(
        f"\n[{assessment.status.value}] {result.symbol} "
        f"{assessment.signal.value} | {assessment.strategy} | "
        f"score {assessment.score}/100"
    )
    if result.structure:
        print(
            f"Primary {result.timeframe}: "
            f"{result.structure.regime.value} | "
            f"Volume: {result.structure.volume_ratio:.2f}x | "
            f"ATR: {result.structure.atr_pct * 100:.2f}%"
        )

    if result.confirmations:
        context = []
        for item in result.confirmations:
            regime = item.regime.value if item.regime else "UNAVAILABLE"
            if item.aligned is True:
                marker = "aligned"
            elif item.aligned is False:
                marker = "conflict"
            else:
                marker = "neutral"
            context.append(
                f"{item.timeframe}: {regime} ({marker})"
            )
        print("HTF context: " + " | ".join(context))

    if result.entry_trigger:
        trigger = result.entry_trigger
        state = (
            "confirmed"
            if trigger.confirmed is True
            else "waiting"
            if trigger.confirmed is False
            else "unavailable"
        )
        regime = (
            trigger.regime.value
            if trigger.regime is not None
            else "UNKNOWN"
        )
        rsi = (
            f"{trigger.rsi:.1f}"
            if trigger.rsi is not None
            else "n/a"
        )
        print(
            f"Entry trigger {trigger.timeframe}: {state} | "
            f"{regime} | RSI {rsi}"
        )

    if result.setup:
        setup = result.setup
        quote = _quote_currency(result.symbol)
        print(f"Entry zone: {setup.entry_low:,.4f} - {setup.entry_high:,.4f} {quote}")
        print(f"Reference:  {setup.reference_entry:,.4f} {quote}")
        print(f"Stop:       {setup.stop_loss:,.4f} {quote}")
        print(f"TP1 (1R):   {setup.take_profit_1:,.4f} {quote}")
        print(f"TP2 (2R):   {setup.take_profit_2:,.4f} {quote}")
        print(
            f"Size:       {setup.position_size:.6f} | "
            f"Notional: {setup.notional_value:,.2f} {quote}"
        )
        print(
            f"Risk:       {setup.actual_risk_amount:,.2f} "
            f"(target {setup.target_risk_amount:,.2f})"
        )

    print(f"Reason: {assessment.reason}")
    if result.data_quality and result.data_quality.warnings:
        print("Data warning: " + "; ".join(result.data_quality.warnings))




def _dispatch_alerts(args, results) -> None:
    names = [
        value.strip().lower()
        for value in args.alert_sinks.split(",")
        if value.strip()
    ]
    if not names:
        return

    if args.alert_cooldown_minutes < 0:
        raise SystemExit(
            "--alert-cooldown-minutes cannot be negative."
        )

    allowed = {"console", "jsonl", "webhook"}
    invalid = sorted(set(names).difference(allowed))
    if invalid:
        raise SystemExit(
            "Unsupported alert sink(s): " + ", ".join(invalid)
        )

    sinks = []
    if "console" in names:
        sinks.append(ConsoleAlertSink())
    if "jsonl" in names:
        sinks.append(JsonlAlertSink(args.alert_jsonl))
    if "webhook" in names:
        if not args.alert_webhook_url:
            raise SystemExit(
                "Webhook alerts require --alert-webhook-url "
                "or TRADING_ALERT_WEBHOOK_URL."
            )
        sinks.append(
            WebhookAlertSink(args.alert_webhook_url)
        )

    ledger = AlertLedger(args.alert_ledger)
    try:
        dispatcher = AlertDispatcher(
            sinks=sinks,
            ledger=ledger,
            cooldown_seconds=int(
                args.alert_cooldown_minutes * 60
            ),
        )
        events = build_scan_alerts(
            results,
            include_forming=args.alert_forming,
            include_errors=True,
        )
        sent, suppressed = dispatcher.dispatch(events)
        print(
            f"Alerts: sent {sent}, suppressed {suppressed} "
            f"by cooldown."
        )
    finally:
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.risk_percent <= 0 or args.risk_percent > 100:
        raise SystemExit("--risk-percent must be between 0 and 100.")
    if args.min_score < 0 or args.min_score > 100:
        raise SystemExit("--min-score must be between 0 and 100.")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.auto_universe:
        quotes = tuple(
            value.strip().upper()
            for value in args.quote_currencies.split(",")
            if value.strip()
        )
        try:
            selection = DynamicMarketUniverse(
                exchange_id=args.exchange,
                market_type=args.market_type,
            ).select(
                UniverseConfig(
                    market_type=args.market_type,
                    quote_currencies=quotes,
                    max_markets=args.max_markets,
                    min_quote_volume=args.min_quote_volume,
                    max_spread_bps=args.max_spread_bps,
                )
            )
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc

        symbols = selection.symbols
        if not symbols:
            raise SystemExit(
                "Auto-universe found no markets matching the filters."
            )

        print(
            f"Auto-universe: selected {len(symbols)} of "
            f"{selection.eligible_markets} eligible markets "
            f"from {selection.total_markets} exchange markets."
        )
        for candidate in selection.candidates:
            spread = (
                f"{candidate.spread_bps:.1f} bps"
                if candidate.spread_bps is not None
                else "spread n/a"
            )
            print(
                f"  {candidate.symbol}: "
                f"24h quote volume {candidate.quote_volume:,.0f} | "
                f"{spread}"
            )
        for warning in selection.warnings:
            print(f"Universe warning: {warning}")

    strategy_names = tuple(
        value.strip()
        for value in args.strategies.split(",")
        if value.strip()
    )
    confirmation_timeframes = tuple(
        value.strip()
        for value in args.confirm_timeframes.split(",")
        if value.strip()
    )
    entry_timeframe = args.entry_timeframe.strip() or None

    try:
        scanner = MarketScanner(
            exchange_id=args.exchange,
            market_type=args.market_type,
            timeframe=args.timeframe,
            confirmation_timeframes=confirmation_timeframes,
            entry_timeframe=entry_timeframe,
            candle_limit=args.limit,
            account_equity=args.equity,
            risk_percent=args.risk_percent / 100,
            min_confirmed_score=args.min_score,
            strategy_names=strategy_names,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    results = scanner.scan(symbols)

    context_label = (
        ",".join(confirmation_timeframes)
        if confirmation_timeframes
        else "none"
    )
    print(
        f"\n{args.exchange.upper()} | {args.market_type} | "
        f"{args.timeframe} | strategies {','.join(strategy_names)} | "
        f"HTF {context_label} | "
        f"scanned {len(results)} market(s)"
    )
    visible = 0
    for result in results:
        if (
            result.assessment
            and result.assessment.status != SetupStatus.NO_TRADE
        ) or result.error or args.show_no_trade:
            _print_result(result, args.show_no_trade)
            visible += 1

    if visible == 0:
        print("\nNo confirmed or forming setups found.")

    _dispatch_alerts(args, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
