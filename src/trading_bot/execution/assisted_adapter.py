from collections.abc import Callable
from uuid import uuid4

import pandas as pd

from trading_bot.assisted.broker import BrokerAdapter
from trading_bot.assisted.models import (
    BrokerCapabilities,
    ExecutionReceipt,
    PreparedOrder,
    TradeIntent,
)
from trading_bot.execution.gateway import (
    ImmediateFillOrderGateway,
    OrderGateway,
)
from trading_bot.execution.manager import OrderManager
from trading_bot.execution.models import ManagedOrderStatus
from trading_bot.execution.store import OrderStore


class ManagedDryRunBrokerAdapter(BrokerAdapter):
    """Assisted broker that persists the order lifecycle before dry-run fill."""

    def __init__(
        self,
        store: OrderStore,
        gateway: OrderGateway | None = None,
        now_fn: Callable[[], pd.Timestamp] | None = None,
    ):
        self.store = store
        self.gateway = (
            gateway
            or ImmediateFillOrderGateway()
        )
        self.manager = OrderManager(
            store,
            self.gateway,
        )
        self._now_fn = now_fn or (
            lambda: pd.Timestamp.now(tz="UTC")
        )
        self._capabilities = BrokerCapabilities(
            broker_id="managed-dry-run",
            supported_market_types=(
                "spot",
                "swap",
                "future",
            ),
            supports_long=True,
            supports_short=True,
            live_execution=False,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def _prepared(
        self,
        intent: TradeIntent,
    ) -> PreparedOrder:
        return PreparedOrder(
            intent_id=intent.intent_id,
            exchange_id=intent.exchange_id,
            market_type=intent.market_type,
            symbol=intent.symbol,
            order_type="limit",
            side=(
                "buy"
                if intent.signal == "LONG"
                else "sell"
            ),
            amount=float(
                intent.plan["position_size"]
            ),
            price=float(
                intent.plan["reference_entry"]
            ),
            client_order_id=intent.intent_id,
        )

    def execute(
        self,
        intent: TradeIntent,
    ) -> ExecutionReceipt:
        self.validate_intent(intent)
        timestamp = self._now_fn()

        managed, _ = self.manager.prepare(
            self._prepared(intent),
            now=timestamp,
        )
        managed = self.manager.submit(
            managed.order_id,
            now=timestamp,
        )

        if managed.status not in {
            ManagedOrderStatus.SUBMITTED,
            ManagedOrderStatus.PARTIALLY_FILLED,
            ManagedOrderStatus.FILLED,
        }:
            raise RuntimeError(
                "Managed dry-run order did not reach an accepted state."
            )

        return ExecutionReceipt(
            receipt_id=uuid4().hex,
            intent_id=intent.intent_id,
            broker_id=self.capabilities.broker_id,
            executed_at=timestamp,
            mode="MANAGED_DRY_RUN",
            accepted=True,
            external_order_id=managed.external_order_id,
            message=(
                f"Managed order {managed.order_id} reached "
                f"{managed.status.value}; no external exchange was contacted."
            ),
            plan_hash=intent.plan_hash,
        )
