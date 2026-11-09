import argparse
import json

from trading_bot.assisted import (
    IntentStatus,
    IntentStore,
    queue_scan_results,
)
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)


def _csv_values(raw: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in raw.split(",")
        if value.strip()
    )


def _add_queue_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--market-type", default="spot")
    parser.add_argument(
        "--symbols",
        default="BTC/USD,ETH/USD",
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
    )
    parser.add_argument("--equity", type=float, default=10_000)
    parser.add_argument("--risk-percent", type=float, default=1.0)
    parser.add_argument("--min-score", type=int, default=70)
    parser.add_argument(
        "--strategies",
        default="trend_continuation",
    )
    parser.add_argument(
        "--expires-after-bars",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Queue and audit human approval of trade plans. "
            "Approved intents are not executed by this command."
        )
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    queue = subparsers.add_parser("queue")
    _add_queue_args(queue)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )
    list_parser.add_argument(
        "--status",
        default="PENDING_APPROVAL",
        help="Status name or ALL.",
    )
    list_parser.add_argument("--limit", type=int, default=50)

    show = subparsers.add_parser("show")
    show.add_argument("intent_id")
    show.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )

    approve = subparsers.add_parser("approve")
    approve.add_argument("intent_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")
    approve.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )

    reject = subparsers.add_parser("reject")
    reject.add_argument("intent_id")
    reject.add_argument("--actor", required=True)
    reject.add_argument("--note", default="")
    reject.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )

    decisions = subparsers.add_parser("decisions")
    decisions.add_argument("--intent-id")
    decisions.add_argument("--limit", type=int, default=100)
    decisions.add_argument(
        "--store",
        default="data/assisted/assisted.db",
    )

    return parser


def _resolve_symbols(args) -> list[str]:
    if not args.auto_universe:
        symbols = list(_csv_values(args.symbols))
        if not symbols:
            raise SystemExit("Provide at least one symbol.")
        return symbols

    quotes = tuple(
        value.upper()
        for value in _csv_values(args.quote_currencies)
    )
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
    if not selection.symbols:
        raise SystemExit(
            "Auto-universe found no eligible markets."
        )
    return selection.symbols


def _queue(args) -> int:
    if not 0 < args.risk_percent <= 100:
        raise SystemExit(
            "--risk-percent must be between 0 and 100."
        )
    if args.expires_after_bars <= 0:
        raise SystemExit(
            "--expires-after-bars must be positive."
        )

    symbols = _resolve_symbols(args)
    scanner = MarketScanner(
        exchange_id=args.exchange,
        market_type=args.market_type,
        timeframe=args.timeframe,
        confirmation_timeframes=_csv_values(
            args.confirm_timeframes
        ),
        account_equity=args.equity,
        risk_percent=args.risk_percent / 100,
        min_confirmed_score=args.min_score,
        strategy_names=_csv_values(args.strategies),
    )
    results = scanner.scan(symbols)

    store = IntentStore(args.store)
    try:
        report = queue_scan_results(
            store=store,
            results=results,
            expires_after_bars=args.expires_after_bars,
        )
        pending = store.list(
            statuses=(IntentStatus.PENDING_APPROVAL,),
            limit=50,
        )
    finally:
        store.close()

    print(
        "Assisted queue | "
        f"queued={report.queued} "
        f"duplicates={report.duplicates} "
        f"stale={report.stale} "
        f"unsupported={report.unsupported} "
        f"ignored={report.ignored}"
    )
    for intent in pending:
        print(
            f"{intent.intent_id} | {intent.symbol} "
            f"{intent.signal} | {intent.strategy} | "
            f"score {intent.score}/100 | "
            f"expires {intent.expires_at}"
        )
    return 0


def _parse_status(value: str):
    if value.upper() == "ALL":
        return None
    try:
        return (IntentStatus(value.upper()),)
    except ValueError as exc:
        raise SystemExit(
            f"Unknown intent status: {value}"
        ) from exc


def _list(args) -> int:
    store = IntentStore(args.store)
    try:
        intents = store.list(
            statuses=_parse_status(args.status),
            limit=args.limit,
        )
    finally:
        store.close()

    if not intents:
        print("No trade intents found.")
        return 0

    for intent in intents:
        print(
            f"{intent.intent_id} | {intent.status.value} | "
            f"{intent.symbol} {intent.signal} | "
            f"{intent.strategy} | score {intent.score}/100 | "
            f"signal {intent.signal_time} | "
            f"expires {intent.expires_at}"
        )
    return 0


def _show(args) -> int:
    store = IntentStore(args.store)
    try:
        store.expire_stale()
        intent = store.get(args.intent_id)
        decisions = store.decisions(
            args.intent_id,
            limit=100,
        )
    finally:
        store.close()

    if intent is None:
        raise SystemExit(
            f"Unknown intent: {args.intent_id}"
        )

    print(
        json.dumps(
            {
                "intent_id": intent.intent_id,
                "status": intent.status.value,
                "plan_hash": intent.plan_hash,
                "created_at": intent.created_at.isoformat(),
                "signal_time": intent.signal_time.isoformat(),
                "expires_at": intent.expires_at.isoformat(),
                "plan": intent.plan,
                "decisions": [
                    {
                        "decision": decision.decision,
                        "actor": decision.actor,
                        "note": decision.note,
                        "decided_at": (
                            decision.decided_at.isoformat()
                        ),
                    }
                    for decision in decisions
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _decide(args, approve: bool) -> int:
    store = IntentStore(args.store)
    try:
        try:
            intent = (
                store.approve(
                    args.intent_id,
                    actor=args.actor,
                    note=args.note,
                )
                if approve
                else store.reject(
                    args.intent_id,
                    actor=args.actor,
                    note=args.note,
                )
            )
        except (KeyError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    finally:
        store.close()

    print(
        f"{intent.intent_id} -> {intent.status.value} "
        f"by {args.actor}. No exchange order was placed."
    )
    return 0


def _decisions(args) -> int:
    store = IntentStore(args.store)
    try:
        decisions = store.decisions(
            intent_id=args.intent_id,
            limit=args.limit,
        )
    finally:
        store.close()

    if not decisions:
        print("No intent decisions recorded.")
        return 0

    for decision in decisions:
        print(
            f"{decision.decided_at} | "
            f"{decision.intent_id} | "
            f"{decision.decision} | "
            f"{decision.actor} | {decision.note}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "queue":
        return _queue(args)
    if args.command == "list":
        return _list(args)
    if args.command == "show":
        return _show(args)
    if args.command == "approve":
        return _decide(args, True)
    if args.command == "reject":
        return _decide(args, False)
    if args.command == "decisions":
        return _decisions(args)

    raise SystemExit("Unknown command.")


if __name__ == "__main__":
    raise SystemExit(main())
