from collections.abc import Callable
from uuid import uuid4

import pandas as pd

from trading_bot.assisted.broker import BrokerAdapter
from trading_bot.assisted.ccxt_broker import (
    AuthenticatedCCXTBrokerAdapter,
)
from trading_bot.assisted.credentials import (
    EnvironmentCredentialProvider,
)
from trading_bot.assisted.models import (
    BrokerCapabilities,
    ExecutionReceipt,
    IntentStatus,
    TradeIntent,
)
from trading_bot.execution.ccxt_gateway import (
    CCXTOrderGateway,
)
from trading_bot.execution.control import (
    LiveExecutionGuard,
)
from trading_bot.execution.manager import OrderManager
from trading_bot.execution.models import (
    ManagedOrderStatus,
)
from trading_bot.execution.store import OrderStore


class ManagedLiveBrokerAdapter(BrokerAdapter):
    """Live-capable broker with persistent idempotent order management.

    Submission is impossible unless LiveExecutionGuard is both environment-
    enabled and persistently armed. Attached SL/TP support is required.
    """

    def __init__(
        self,
        *,
        exchange_id: str,
        market_type: str,
        store: OrderStore,
        guard: LiveExecutionGuard,
        credential_provider: (
            EnvironmentCredentialProvider | None
        ) = None,
        exchange_factory: Callable[[dict], object] | None = None,
        now_fn: Callable[[], pd.Timestamp] | None = None,
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.store = store
        self.guard = guard
        self._now_fn = now_fn or (
            lambda: pd.Timestamp.now(tz="UTC")
        )

        self.preparer = AuthenticatedCCXTBrokerAdapter(
            exchange_id=exchange_id,
            market_type=market_type,
            credential_provider=credential_provider,
            exchange_factory=exchange_factory,
            require_attached_protection=True,
            require_client_order_id=True,
        )
        self.gateway = CCXTOrderGateway(
            self.preparer.exchange,
            guard=guard,
        )
        self.manager = OrderManager(
            store,
            self.gateway,
        )
        self._capabilities = BrokerCapabilities(
            broker_id=f"managed-live:{exchange_id}",
            supported_market_types=(market_type,),
            supports_long=True,
            supports_short=market_type != "spot",
            live_execution=True,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def execute(
        self,
        intent: TradeIntent,
    ) -> ExecutionReceipt:
        self.validate_intent(intent)
        if intent.exchange_id != self.exchange_id:
            raise ValueError(
                "Intent exchange does not match live broker."
            )
        if intent.market_type != self.market_type:
            raise ValueError(
                "Intent market type does not match live broker."
            )

        timestamp = self._now_fn()
        if intent.status != IntentStatus.APPROVED:
            raise ValueError(
                "Live execution requires an APPROVED assisted intent."
            )
        if timestamp >= intent.expires_at:
            raise ValueError(
                "Approved intent expired before live submission."
            )

        prepared = self.preparer.prepare_order(
            intent
        )
        managed, _ = self.manager.prepare(
            prepared,
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
                "Live order did not reach a confirmed accepted state. "
                f"Managed status is {managed.status.value}; reconcile "
                "before any retry."
            )

        return ExecutionReceipt(
            receipt_id=uuid4().hex,
            intent_id=intent.intent_id,
            broker_id=self.capabilities.broker_id,
            executed_at=timestamp,
            mode="LIVE",
            accepted=True,
            external_order_id=managed.external_order_id,
            message=(
                f"Managed live order {managed.order_id} reached "
                f"{managed.status.value}."
            ),
            plan_hash=intent.plan_hash,
        )
