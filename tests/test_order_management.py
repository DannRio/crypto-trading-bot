import pytest

from trading_bot.assisted import PreparedOrder
from trading_bot.execution import (
    InMemoryOrderGateway,
    ManagedOrderStatus,
    OrderManager,
    OrderStore,
)


def prepared() -> PreparedOrder:
    return PreparedOrder(
        intent_id="intent-1",
        exchange_id="kucoin",
        market_type="spot",
        symbol="BTC/USDT",
        order_type="limit",
        side="buy",
        amount=1.25,
        price=100.0,
        client_order_id="intent-1",
    )


def test_prepare_is_idempotent_and_persistent(tmp_path):
    path = tmp_path / "orders.db"
    store = OrderStore(path)
    manager = OrderManager(
        store,
        InMemoryOrderGateway(),
    )

    first, created = manager.prepare(prepared())
    second, created_again = manager.prepare(prepared())

    assert created is True
    assert created_again is False
    assert second.order_id == first.order_id
    assert second.status == ManagedOrderStatus.PREPARED

    store.close()

    restored = OrderStore(path)
    loaded = restored.get(first.order_id)
    assert loaded is not None
    assert loaded.intent_id == "intent-1"
    assert loaded.status == ManagedOrderStatus.PREPARED
    restored.close()


def test_submit_is_idempotent_after_confirmation(tmp_path):
    store = OrderStore(tmp_path / "orders.db")
    gateway = InMemoryOrderGateway()
    manager = OrderManager(store, gateway)
    order, _ = manager.prepare(prepared())

    submitted = manager.submit(order.order_id)
    again = manager.submit(order.order_id)

    assert submitted.status == ManagedOrderStatus.SUBMITTED
    assert again.external_order_id == submitted.external_order_id
    assert len(gateway._by_external) == 1

    events = store.events(order.order_id)
    assert [event.event_type for event in events] == [
        "PREPARED",
        "SUBMIT_STARTED",
        "SUBMIT_CONFIRMED",
    ]
    store.close()


class AmbiguousSubmitGateway(InMemoryOrderGateway):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    def submit(self, order):
        snapshot = super().submit(order)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError(
                "Network timed out after exchange accepted order."
            )
        return snapshot


def test_ambiguous_submit_requires_reconciliation_not_retry(
    tmp_path,
):
    store = OrderStore(tmp_path / "orders.db")
    gateway = AmbiguousSubmitGateway()
    manager = OrderManager(store, gateway)
    order, _ = manager.prepare(prepared())

    with pytest.raises(TimeoutError):
        manager.submit(order.order_id)

    unknown = store.get(order.order_id)
    assert unknown is not None
    assert unknown.status == ManagedOrderStatus.UNKNOWN

    with pytest.raises(RuntimeError, match="reconcile"):
        manager.submit(order.order_id)

    reconciled = manager.reconcile(order.order_id)
    assert reconciled.status == ManagedOrderStatus.SUBMITTED
    assert reconciled.external_order_id is not None
    assert len(gateway._by_external) == 1

    again = manager.submit(order.order_id)
    assert again.external_order_id == reconciled.external_order_id
    assert len(gateway._by_external) == 1
    store.close()


def test_partial_and_full_fill_reconcile(tmp_path):
    store = OrderStore(tmp_path / "orders.db")
    gateway = InMemoryOrderGateway()
    manager = OrderManager(store, gateway)
    order, _ = manager.prepare(prepared())
    submitted = manager.submit(order.order_id)

    gateway.set_fill(
        submitted.external_order_id,
        filled_amount=0.5,
        average_price=100.1,
        total_amount=1.25,
    )
    partial = manager.reconcile(order.order_id)
    assert partial.status == ManagedOrderStatus.PARTIALLY_FILLED
    assert partial.filled_amount == pytest.approx(0.5)
    assert partial.remaining_amount == pytest.approx(0.75)

    gateway.set_fill(
        submitted.external_order_id,
        filled_amount=1.25,
        average_price=100.2,
        total_amount=1.25,
    )
    filled = manager.reconcile(order.order_id)
    assert filled.status == ManagedOrderStatus.FILLED
    assert filled.terminal is True
    assert filled.remaining_amount == 0
    store.close()


def test_submitted_order_can_be_cancelled(tmp_path):
    store = OrderStore(tmp_path / "orders.db")
    gateway = InMemoryOrderGateway()
    manager = OrderManager(store, gateway)
    order, _ = manager.prepare(prepared())
    submitted = manager.submit(order.order_id)

    cancelled = manager.cancel(submitted.order_id)

    assert cancelled.status == ManagedOrderStatus.CANCELLED
    assert cancelled.terminal is True
    events = store.events(order.order_id)
    assert events[-2].event_type == "CANCEL_STARTED"
    assert events[-1].event_type == "CANCEL_CONFIRMED"
    store.close()


def test_active_orders_survive_restart(tmp_path):
    path = tmp_path / "orders.db"
    gateway = InMemoryOrderGateway()

    store = OrderStore(path)
    manager = OrderManager(store, gateway)
    order, _ = manager.prepare(prepared())
    manager.submit(order.order_id)
    store.close()

    restored = OrderStore(path)
    active = restored.active_orders()

    assert len(active) == 1
    assert active[0].status == ManagedOrderStatus.SUBMITTED
    assert active[0].external_order_id is not None
    restored.close()
