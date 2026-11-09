from trading_bot.assisted.models import PreparedOrder
from trading_bot.execution.gateway import OrderGateway
from trading_bot.execution.models import (
    ExchangeOrderSnapshot,
    ManagedOrder,
    ManagedOrderStatus,
    TERMINAL_ORDER_STATUSES,
)
from trading_bot.execution.store import OrderStore


class OrderManager:
    def __init__(
        self,
        store: OrderStore,
        gateway: OrderGateway,
    ):
        self.store = store
        self.gateway = gateway

    def prepare(
        self,
        order: PreparedOrder,
        now=None,
    ) -> tuple[ManagedOrder, bool]:
        return self.store.create_or_get(
            order,
            now=now,
        )

    @staticmethod
    def _prepared(
        order: ManagedOrder,
    ) -> PreparedOrder:
        return PreparedOrder(
            intent_id=order.intent_id,
            exchange_id=order.exchange_id,
            market_type=order.market_type,
            symbol=order.symbol,
            order_type=order.order_type,
            side=order.side,
            amount=order.amount,
            price=order.limit_price,
            client_order_id=order.client_order_id,
            params=dict(order.params or {}),
        )

    def _apply_snapshot(
        self,
        order: ManagedOrder,
        snapshot: ExchangeOrderSnapshot,
        allowed_from: tuple[
            ManagedOrderStatus,
            ...,
        ],
        *,
        event_type: str,
        now=None,
    ) -> ManagedOrder:
        if (
            snapshot.client_order_id is not None
            and snapshot.client_order_id
            != order.client_order_id
        ):
            raise ValueError(
                "Exchange snapshot client-order id does not "
                "match the managed order."
            )

        if (
            order.external_order_id is not None
            and snapshot.external_order_id
            != order.external_order_id
        ):
            raise ValueError(
                "Exchange snapshot external-order id changed."
            )

        if snapshot.status not in {
            ManagedOrderStatus.SUBMITTED,
            ManagedOrderStatus.PARTIALLY_FILLED,
            ManagedOrderStatus.FILLED,
            ManagedOrderStatus.CANCELLED,
            ManagedOrderStatus.REJECTED,
            ManagedOrderStatus.UNKNOWN,
        }:
            raise ValueError(
                "Gateway returned an unsupported normalized order state: "
                + snapshot.status.value
            )

        return self.store.transition(
            order.order_id,
            snapshot.status,
            allowed_from=allowed_from,
            external_order_id=(
                snapshot.external_order_id
            ),
            filled_amount=snapshot.filled_amount,
            average_price=snapshot.average_price,
            last_error=None,
            event_type=event_type,
            message=snapshot.message,
            now=now,
        )

    def apply_snapshot(
        self,
        order_id: str,
        snapshot: ExchangeOrderSnapshot,
        *,
        event_type: str = "RECONCILED",
        now=None,
    ) -> ManagedOrder:
        order = self.store.get(order_id)
        if order is None:
            raise KeyError(
                f"Unknown managed order: {order_id}"
            )
        return self._apply_snapshot(
            order,
            snapshot,
            allowed_from=(order.status,),
            event_type=event_type,
            now=now,
        )

    def submit(
        self,
        order_id: str,
        now=None,
    ) -> ManagedOrder:
        order = self.store.get(order_id)
        if order is None:
            raise KeyError(
                f"Unknown managed order: {order_id}"
            )

        if order.status in {
            ManagedOrderStatus.SUBMITTED,
            ManagedOrderStatus.PARTIALLY_FILLED,
            ManagedOrderStatus.FILLED,
            ManagedOrderStatus.CANCELLED,
            ManagedOrderStatus.REJECTED,
        }:
            return order

        if order.status in {
            ManagedOrderStatus.SUBMITTING,
            ManagedOrderStatus.UNKNOWN,
            ManagedOrderStatus.CANCEL_PENDING,
        }:
            raise RuntimeError(
                f"Order {order_id} is {order.status.value}; "
                "reconcile it before any further submission."
            )

        if order.status != ManagedOrderStatus.PREPARED:
            raise ValueError(
                f"Order {order_id} cannot be submitted from "
                f"{order.status.value}."
            )

        submitting = self.store.transition(
            order_id,
            ManagedOrderStatus.SUBMITTING,
            allowed_from=(ManagedOrderStatus.PREPARED,),
            event_type="SUBMIT_STARTED",
            message=(
                "Submission started; duplicate submission is now blocked."
            ),
            now=now,
        )

        try:
            snapshot = self.gateway.submit(
                self._prepared(submitting)
            )
        except Exception as exc:
            self.store.transition(
                order_id,
                ManagedOrderStatus.UNKNOWN,
                allowed_from=(
                    ManagedOrderStatus.SUBMITTING,
                ),
                last_error=(
                    f"{type(exc).__name__}: {exc}"
                ),
                event_type="SUBMIT_AMBIGUOUS",
                message=(
                    "Submission outcome is unknown; reconciliation "
                    "is required before retry."
                ),
                now=now,
            )
            raise

        return self._apply_snapshot(
            submitting,
            snapshot,
            allowed_from=(
                ManagedOrderStatus.SUBMITTING,
            ),
            event_type="SUBMIT_CONFIRMED",
            now=now,
        )

    def reconcile(
        self,
        order_id: str,
        now=None,
    ) -> ManagedOrder:
        order = self.store.get(order_id)
        if order is None:
            raise KeyError(
                f"Unknown managed order: {order_id}"
            )

        if order.status in TERMINAL_ORDER_STATUSES:
            return order

        if order.external_order_id is not None:
            snapshot = self.gateway.fetch(
                order.external_order_id,
                order.symbol,
            )
        else:
            snapshot = (
                self.gateway.find_by_client_order_id(
                    order.client_order_id,
                    order.symbol,
                )
            )

        if snapshot is None:
            return self.store.transition(
                order_id,
                order.status,
                allowed_from=(order.status,),
                last_error=order.last_error,
                event_type="RECONCILE_NOT_FOUND",
                message=(
                    "No exchange order was found. State remains "
                    f"{order.status.value}; operator resolution is required."
                ),
                now=now,
            )

        return self._apply_snapshot(
            order,
            snapshot,
            allowed_from=(order.status,),
            event_type="RECONCILED",
            now=now,
        )

    def cancel(
        self,
        order_id: str,
        now=None,
    ) -> ManagedOrder:
        order = self.store.get(order_id)
        if order is None:
            raise KeyError(
                f"Unknown managed order: {order_id}"
            )

        if order.status in TERMINAL_ORDER_STATUSES:
            return order
        if order.status not in {
            ManagedOrderStatus.SUBMITTED,
            ManagedOrderStatus.PARTIALLY_FILLED,
        }:
            raise RuntimeError(
                f"Order {order_id} is {order.status.value}; "
                "only submitted orders can be cancelled."
            )
        if order.external_order_id is None:
            raise RuntimeError(
                "Submitted order has no external order id; "
                "reconcile before cancellation."
            )

        cancelling = self.store.transition(
            order_id,
            ManagedOrderStatus.CANCEL_PENDING,
            allowed_from=(
                ManagedOrderStatus.SUBMITTED,
                ManagedOrderStatus.PARTIALLY_FILLED,
            ),
            event_type="CANCEL_STARTED",
            message="Cancellation request started.",
            now=now,
        )

        try:
            snapshot = self.gateway.cancel(
                cancelling.external_order_id,
                cancelling.symbol,
            )
        except Exception as exc:
            self.store.transition(
                order_id,
                ManagedOrderStatus.UNKNOWN,
                allowed_from=(
                    ManagedOrderStatus.CANCEL_PENDING,
                ),
                last_error=(
                    f"{type(exc).__name__}: {exc}"
                ),
                event_type="CANCEL_AMBIGUOUS",
                message=(
                    "Cancellation outcome is unknown; reconcile "
                    "before further action."
                ),
                now=now,
            )
            raise

        return self._apply_snapshot(
            cancelling,
            snapshot,
            allowed_from=(
                ManagedOrderStatus.CANCEL_PENDING,
            ),
            event_type="CANCEL_CONFIRMED",
            now=now,
        )
