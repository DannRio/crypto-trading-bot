import argparse
from dataclasses import asdict
import json
from pathlib import Path

import pandas as pd

from trading_bot.assisted import (
    AuthenticatedCCXTBrokerAdapter,
    IntentStatus,
    IntentStore,
)
from trading_bot.execution.ccxt_gateway import (
    CCXTOrderGateway,
)
from trading_bot.execution.control import (
    ARM_CONFIRMATION,
    RESET_CONFIRMATION,
    KillSwitchService,
    LiveExecutionGuard,
)
from trading_bot.execution.live_adapter import (
    ManagedLiveBrokerAdapter,
)
from trading_bot.execution.manager import OrderManager
from trading_bot.execution.reconciliation import (
    ReconciliationService,
)
from trading_bot.execution.store import OrderStore


SUBMIT_CONFIRMATION = "SUBMIT_LIVE"


def _json(value) -> str:
    return json.dumps(
        value,
        indent=2,
        default=str,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-execution",
        description=(
            "Operator controls for guarded live execution. "
            "Live submission is disabled unless environment and "
            "persistent arm gates both permit it."
        ),
    )
    parser.add_argument(
        "--order-db",
        default="data/orders/live_orders.db",
    )
    parser.add_argument(
        "--assisted-db",
        default="data/assisted/assisted.db",
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    sub.add_parser("status")

    arm = sub.add_parser("arm")
    arm.add_argument("--actor", required=True)
    arm.add_argument("--confirm", required=True)

    disarm = sub.add_parser("disarm")
    disarm.add_argument("--actor", required=True)
    disarm.add_argument("--reason", default="")

    reset = sub.add_parser("reset-halt")
    reset.add_argument("--actor", required=True)
    reset.add_argument("--confirm", required=True)

    approve = sub.add_parser("approve-intent")
    approve.add_argument("--intent-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")

    probe = sub.add_parser("probe")
    probe.add_argument("--exchange", required=True)
    probe.add_argument(
        "--market-type",
        default="spot",
    )

    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--exchange", required=True)
    reconcile.add_argument(
        "--market-type",
        default="spot",
    )
    reconcile.add_argument(
        "--cancel-orphans",
        action="store_true",
    )

    trip = sub.add_parser("trip")
    trip.add_argument("--actor", required=True)
    trip.add_argument("--reason", required=True)
    trip.add_argument("--exchange")
    trip.add_argument(
        "--market-type",
        default="spot",
    )
    trip.add_argument(
        "--no-cancel",
        action="store_true",
    )

    submit = sub.add_parser(
        "live-submit-intent"
    )
    submit.add_argument(
        "--intent-id",
        required=True,
    )
    submit.add_argument(
        "--exchange",
        required=True,
    )
    submit.add_argument(
        "--market-type",
        default="spot",
    )
    submit.add_argument(
        "--confirm",
        required=True,
    )

    return parser


def _authenticated_gateway(
    *,
    exchange: str,
    market_type: str,
    order_store: OrderStore,
):
    order_store.bind_execution_context(
        exchange_id=exchange,
        market_type=market_type,
    )
    guard = LiveExecutionGuard(
        order_store
    )
    broker = AuthenticatedCCXTBrokerAdapter(
        exchange_id=exchange,
        market_type=market_type,
    )
    gateway = CCXTOrderGateway(
        broker.exchange,
        guard=guard,
    )
    manager = OrderManager(
        order_store,
        gateway,
    )
    return guard, gateway, manager


def main(argv=None) -> int:
    args = build_parser().parse_args(
        argv
    )
    order_store = OrderStore(
        Path(args.order_db)
    )

    try:
        guard = LiveExecutionGuard(
            order_store
        )

        if args.command == "status":
            state = guard.status()
            payload = {
                "environment_enabled": (
                    guard.environment_enabled
                ),
                "control": asdict(state),
                "active_orders": len(
                    order_store.active_orders()
                ),
            }
            print(_json(payload))
            return 0

        if args.command == "arm":
            state = guard.arm(
                actor=args.actor,
                confirmation=args.confirm,
            )
            print(_json(asdict(state)))
            return 0

        if args.command == "disarm":
            state = guard.disarm(
                actor=args.actor,
                reason=args.reason,
            )
            print(_json(asdict(state)))
            return 0

        if args.command == "reset-halt":
            state = guard.reset_halt(
                actor=args.actor,
                confirmation=args.confirm,
            )
            print(_json(asdict(state)))
            return 0

        if args.command == "approve-intent":
            intents = IntentStore(
                Path(args.assisted_db)
            )
            try:
                intent = intents.approve(
                    args.intent_id,
                    actor=args.actor,
                    note=args.note,
                )
                print(_json(asdict(intent)))
                return 0
            finally:
                intents.close()

        if args.command == "probe":
            broker = AuthenticatedCCXTBrokerAdapter(
                exchange_id=args.exchange,
                market_type=args.market_type,
            )
            balance = broker.validate_private_access()
            print(
                _json(
                    {
                        "exchange": args.exchange,
                        "market_type": args.market_type,
                        "private_access": "ok",
                        "balance_keys": sorted(
                            balance.keys()
                        )
                        if isinstance(
                            balance,
                            dict,
                        )
                        else [],
                    }
                )
            )
            return 0

        if args.command == "reconcile":
            (
                live_guard,
                gateway,
                manager,
            ) = _authenticated_gateway(
                exchange=args.exchange,
                market_type=args.market_type,
                order_store=order_store,
            )
            report = ReconciliationService(
                order_store,
                manager,
                gateway,
                live_guard,
            ).reconcile_all(
                cancel_orphans=(
                    args.cancel_orphans
                ),
            )
            print(_json(asdict(report)))
            return 0

        if args.command == "trip":
            guard.trip(
                actor=args.actor,
                reason=args.reason,
            )
            if args.no_cancel:
                print(
                    _json(
                        {
                            "halted": True,
                            "cancel_open_orders": False,
                        }
                    )
                )
                return 0
            if not args.exchange:
                raise ValueError(
                    "--exchange is required unless --no-cancel "
                    "is used."
                )
            (
                live_guard,
                gateway,
                manager,
            ) = _authenticated_gateway(
                exchange=args.exchange,
                market_type=args.market_type,
                order_store=order_store,
            )
            report = KillSwitchService(
                order_store,
                manager,
                live_guard,
            ).trip(
                actor=args.actor,
                reason=args.reason,
                cancel_open_orders=True,
            )
            print(_json(asdict(report)))
            return 0

        if args.command == "live-submit-intent":
            if args.confirm != SUBMIT_CONFIRMATION:
                raise ValueError(
                    "Live submit requires confirmation token "
                    f"{SUBMIT_CONFIRMATION}."
                )

            intents = IntentStore(
                Path(args.assisted_db)
            )
            try:
                intents.expire_stale(
                    pd.Timestamp.now(
                        tz="UTC"
                    )
                )
                intent = intents.get(
                    args.intent_id
                )
                if intent is None:
                    raise KeyError(
                        f"Unknown intent: {args.intent_id}"
                    )
                if (
                    intent.status
                    != IntentStatus.APPROVED
                ):
                    raise ValueError(
                        "Live submit requires an APPROVED intent; "
                        f"current status is {intent.status.value}."
                    )
                if intent.exchange_id != args.exchange:
                    raise ValueError(
                        "Intent exchange does not match --exchange."
                    )
                if intent.market_type != args.market_type:
                    raise ValueError(
                        "Intent market type does not match --market-type."
                    )

                live_broker = (
                    ManagedLiveBrokerAdapter(
                        exchange_id=args.exchange,
                        market_type=args.market_type,
                        store=order_store,
                        guard=guard,
                    )
                )
                receipt = live_broker.execute(
                    intent
                )
                executed = intents.record_execution(
                    receipt
                )
                print(
                    _json(
                        {
                            "intent": asdict(
                                executed
                            ),
                            "receipt": asdict(
                                receipt
                            ),
                        }
                    )
                )
                return 0
            finally:
                intents.close()

        raise RuntimeError(
            f"Unhandled command: {args.command}"
        )
    finally:
        order_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
