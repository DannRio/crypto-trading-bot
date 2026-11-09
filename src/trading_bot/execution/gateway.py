from abc import ABC, abstractmethod
from dataclasses import replace
from uuid import uuid4

from trading_bot.assisted.models import PreparedOrder
from trading_bot.execution.models import (
    ExchangeOrderSnapshot,
    ManagedOrderStatus,
)


class OrderGateway(ABC):
    @abstractmethod
    def submit(
        self,
        order: PreparedOrder,
    ) -> ExchangeOrderSnapshot:
        raise NotImplementedError

    @abstractmethod
    def fetch(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    def find_by_client_order_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    def list_open_orders(
        self,
        symbol: str | None = None,
    ) -> list[ExchangeOrderSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def cancel(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot:
        raise NotImplementedError


class InMemoryOrderGateway(OrderGateway):
    """Deterministic no-network gateway for order-state testing."""

    def __init__(self):
        self._by_external: dict[
            str,
            ExchangeOrderSnapshot,
        ] = {}
        self._by_client: dict[str, str] = {}

    def submit(
        self,
        order: PreparedOrder,
    ) -> ExchangeOrderSnapshot:
        existing_id = self._by_client.get(
            order.client_order_id
        )
        if existing_id is not None:
            return self._by_external[existing_id]

        external_id = f"dry-{uuid4().hex}"
        snapshot = ExchangeOrderSnapshot(
            external_order_id=external_id,
            client_order_id=order.client_order_id,
            status=ManagedOrderStatus.SUBMITTED,
            filled_amount=0.0,
            average_price=None,
            message="Dry-run order accepted.",
        )
        self._by_external[external_id] = snapshot
        self._by_client[
            order.client_order_id
        ] = external_id
        return snapshot

    def fetch(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        return self._by_external.get(
            external_order_id
        )

    def find_by_client_order_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        external_id = self._by_client.get(
            client_order_id
        )
        if external_id is None:
            return None
        return self._by_external[external_id]

    def list_open_orders(
        self,
        symbol: str | None = None,
    ) -> list[ExchangeOrderSnapshot]:
        return [
            snapshot
            for snapshot in self._by_external.values()
            if snapshot.status
            in {
                ManagedOrderStatus.SUBMITTED,
                ManagedOrderStatus.PARTIALLY_FILLED,
            }
        ]

    def cancel(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot:
        current = self._by_external.get(
            external_order_id
        )
        if current is None:
            raise KeyError(
                f"Unknown dry-run order: {external_order_id}"
            )
        if current.status == ManagedOrderStatus.FILLED:
            return current

        updated = replace(
            current,
            status=ManagedOrderStatus.CANCELLED,
            message="Dry-run order cancelled.",
        )
        self._by_external[
            external_order_id
        ] = updated
        return updated

    def set_fill(
        self,
        external_order_id: str,
        filled_amount: float,
        average_price: float,
        total_amount: float,
    ) -> ExchangeOrderSnapshot:
        current = self._by_external[
            external_order_id
        ]
        if filled_amount < 0 or filled_amount > total_amount:
            raise ValueError(
                "filled_amount must be within order amount."
            )
        status = (
            ManagedOrderStatus.FILLED
            if filled_amount >= total_amount
            else ManagedOrderStatus.PARTIALLY_FILLED
            if filled_amount > 0
            else ManagedOrderStatus.SUBMITTED
        )
        updated = replace(
            current,
            status=status,
            filled_amount=filled_amount,
            average_price=average_price,
            message="Dry-run fill state updated.",
        )
        self._by_external[
            external_order_id
        ] = updated
        return updated


class ImmediateFillOrderGateway(InMemoryOrderGateway):
    """No-network gateway that immediately fills accepted orders."""

    def submit(
        self,
        order: PreparedOrder,
    ) -> ExchangeOrderSnapshot:
        snapshot = super().submit(order)
        if snapshot.status == ManagedOrderStatus.FILLED:
            return snapshot
        return self.set_fill(
            snapshot.external_order_id,
            filled_amount=order.amount,
            average_price=order.price,
            total_amount=order.amount,
        )
