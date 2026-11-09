from abc import ABC, abstractmethod
from uuid import uuid4

import pandas as pd

from trading_bot.assisted.models import (
    BrokerCapabilities,
    ExecutionReceipt,
    TradeIntent,
)


class BrokerAdapter(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError

    def validate_intent(self, intent: TradeIntent) -> None:
        capabilities = self.capabilities

        if intent.market_type not in capabilities.supported_market_types:
            raise ValueError(
                f"Broker {capabilities.broker_id} does not support "
                f"market type {intent.market_type}."
            )

        if intent.signal == "LONG" and not capabilities.supports_long:
            raise ValueError(
                f"Broker {capabilities.broker_id} does not support LONG."
            )

        if intent.signal == "SHORT" and not capabilities.supports_short:
            raise ValueError(
                f"Broker {capabilities.broker_id} does not support SHORT."
            )

        if intent.market_type == "spot" and intent.signal == "SHORT":
            raise ValueError(
                "Naked SHORT intents are not executable on spot markets."
            )

    @abstractmethod
    def execute(self, intent: TradeIntent) -> ExecutionReceipt:
        raise NotImplementedError


class DryRunBrokerAdapter(BrokerAdapter):
    """No-network broker used to validate the assisted workflow safely."""

    def __init__(
        self,
        broker_id: str = "dry-run",
        supported_market_types: tuple[str, ...] = (
            "spot",
            "swap",
            "future",
        ),
    ):
        self._capabilities = BrokerCapabilities(
            broker_id=broker_id,
            supported_market_types=supported_market_types,
            supports_long=True,
            supports_short=True,
            live_execution=False,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def execute(self, intent: TradeIntent) -> ExecutionReceipt:
        self.validate_intent(intent)

        return ExecutionReceipt(
            receipt_id=uuid4().hex,
            intent_id=intent.intent_id,
            broker_id=self.capabilities.broker_id,
            executed_at=pd.Timestamp.now(tz="UTC"),
            mode="DRY_RUN",
            accepted=True,
            external_order_id=None,
            message=(
                "Approved intent passed broker capability checks. "
                "No external order was sent."
            ),
            plan_hash=intent.plan_hash,
        )
