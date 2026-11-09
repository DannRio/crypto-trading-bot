from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd


class ManagedOrderStatus(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATUSES = frozenset(
    {
        ManagedOrderStatus.FILLED,
        ManagedOrderStatus.CANCELLED,
        ManagedOrderStatus.REJECTED,
    }
)


@dataclass(frozen=True)
class ExchangeOrderSnapshot:
    external_order_id: str
    client_order_id: str | None
    status: ManagedOrderStatus
    filled_amount: float
    average_price: float | None = None
    symbol: str | None = None
    message: str = ""


@dataclass(frozen=True)
class ManagedOrder:
    order_id: str
    intent_id: str
    exchange_id: str
    market_type: str
    symbol: str
    order_type: str
    side: str
    amount: float
    limit_price: float
    client_order_id: str
    status: ManagedOrderStatus
    external_order_id: str | None
    filled_amount: float
    average_price: float | None
    created_at: pd.Timestamp
    updated_at: pd.Timestamp
    last_error: str | None = None
    params: dict[str, Any] | None = None

    @property
    def remaining_amount(self) -> float:
        return max(0.0, self.amount - self.filled_amount)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    event_type: str
    status: ManagedOrderStatus
    occurred_at: pd.Timestamp
    message: str


@dataclass(frozen=True)
class ExecutionControlState:
    armed: bool
    halted: bool
    halt_reason: str
    updated_by: str
    updated_at: pd.Timestamp

    @property
    def locally_enabled(self) -> bool:
        return self.armed and not self.halted


@dataclass(frozen=True)
class ReconciliationReport:
    local_checked: int
    updated: int
    unchanged: int
    missing_on_exchange: int
    orphaned_exchange_orders: int
    errors: int
    orphaned_client_order_ids: tuple[str, ...] = ()
    error_messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class KillSwitchReport:
    halted: bool
    cancelled_orders: int
    skipped_orders: int
    errors: int
    error_messages: tuple[str, ...] = ()
