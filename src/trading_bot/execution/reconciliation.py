from trading_bot.execution.control import LiveExecutionGuard
from trading_bot.execution.gateway import OrderGateway
from trading_bot.execution.ids import is_bot_client_order_id
from trading_bot.execution.manager import OrderManager
from trading_bot.execution.models import (
    ManagedOrderStatus,
    ReconciliationReport,
)
from trading_bot.execution.store import OrderStore


class ReconciliationService:
    """Reconcile local managed orders with exchange order state."""

    def __init__(
        self,
        store: OrderStore,
        manager: OrderManager,
        gateway: OrderGateway,
        guard: LiveExecutionGuard | None = None,
    ):
        self.store = store
        self.manager = manager
        self.gateway = gateway
        self.guard = guard

    def _lookup_local_order(self, order):
        if order.external_order_id is not None:
            return self.gateway.fetch(
                order.external_order_id,
                order.symbol,
            )
        return self.gateway.find_by_client_order_id(
            order.client_order_id,
            order.symbol,
        )

    def reconcile_all(
        self,
        *,
        now=None,
        halt_on_orphan: bool = True,
        cancel_orphans: bool = False,
        actor: str = "RECONCILER",
    ) -> ReconciliationReport:
        checked = 0
        updated = 0
        unchanged = 0
        missing = 0
        errors: list[str] = []

        for order in self.store.active_orders():
            if order.status == ManagedOrderStatus.PREPARED:
                unchanged += 1
                continue

            checked += 1
            try:
                snapshot = self._lookup_local_order(
                    order
                )
                if snapshot is None:
                    missing += 1
                    self.store.transition(
                        order.order_id,
                        order.status,
                        allowed_from=(order.status,),
                        last_error=order.last_error,
                        event_type="RECONCILE_NOT_FOUND",
                        message=(
                            "Managed order was not found through the "
                            "exchange reconciliation endpoints."
                        ),
                        now=now,
                    )
                    continue

                before = (
                    order.status,
                    order.external_order_id,
                    order.filled_amount,
                    order.average_price,
                )
                after = self.manager.apply_snapshot(
                    order.order_id,
                    snapshot,
                    event_type="RECONCILED",
                    now=now,
                )
                after_state = (
                    after.status,
                    after.external_order_id,
                    after.filled_amount,
                    after.average_price,
                )
                if after_state == before:
                    unchanged += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append(
                    f"{order.order_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        orphaned_ids: list[str] = []
        try:
            exchange_open = (
                self.gateway.list_open_orders()
            )
        except Exception as exc:
            exchange_open = []
            errors.append(
                "open-order inventory: "
                f"{type(exc).__name__}: {exc}"
            )

        for snapshot in exchange_open:
            client_id = snapshot.client_order_id
            if not is_bot_client_order_id(
                client_id
            ):
                continue
            if (
                client_id is not None
                and self.store.get_by_client_order_id(
                    client_id
                )
                is None
            ):
                orphaned_ids.append(client_id)
                if cancel_orphans:
                    try:
                        if not snapshot.symbol:
                            raise RuntimeError(
                                "Orphaned order response has no symbol; "
                                "cannot cancel it safely."
                            )
                        self.gateway.cancel(
                            snapshot.external_order_id,
                            snapshot.symbol,
                        )
                    except Exception as exc:
                        errors.append(
                            "orphan cancel "
                            f"{snapshot.external_order_id}: "
                            f"{type(exc).__name__}: {exc}"
                        )

        if (
            orphaned_ids
            and halt_on_orphan
            and self.guard is not None
        ):
            self.guard.trip(
                actor=actor,
                reason=(
                    "Exchange reconciliation found "
                    f"{len(orphaned_ids)} bot-tagged open order(s) "
                    "without local managed-order records."
                ),
                now=now,
            )

        return ReconciliationReport(
            local_checked=checked,
            updated=updated,
            unchanged=unchanged,
            missing_on_exchange=missing,
            orphaned_exchange_orders=len(
                orphaned_ids
            ),
            errors=len(errors),
            orphaned_client_order_ids=tuple(
                orphaned_ids
            ),
            error_messages=tuple(errors),
        )
