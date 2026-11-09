from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd


class IntentStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    fingerprint: str
    plan_hash: str
    created_at: pd.Timestamp
    signal_time: pd.Timestamp
    expires_at: pd.Timestamp
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str
    strategy: str
    signal: str
    score: int
    status: IntentStatus
    plan: dict[str, Any]


@dataclass(frozen=True)
class IntentDecision:
    intent_id: str
    decision: str
    actor: str
    note: str
    decided_at: pd.Timestamp


@dataclass(frozen=True)
class QueueReport:
    queued: int
    duplicates: int
    stale: int
    unsupported: int
    ignored: int



@dataclass(frozen=True)
class BrokerCapabilities:
    broker_id: str
    supported_market_types: tuple[str, ...]
    supports_long: bool = True
    supports_short: bool = True
    live_execution: bool = False


@dataclass(frozen=True)
class ExecutionReceipt:
    receipt_id: str
    intent_id: str
    broker_id: str
    executed_at: pd.Timestamp
    mode: str
    accepted: bool
    external_order_id: str | None
    message: str
    plan_hash: str
