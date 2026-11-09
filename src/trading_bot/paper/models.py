from dataclasses import dataclass
from enum import Enum

import pandas as pd

from trading_bot.core.enums import Signal


class PaperOrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    timeframe: str
    signal: Signal
    created_at: pd.Timestamp
    reference_entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    position_size: float
    planned_risk: float
    score: int
    regime: str
    strategy: str
    bars_remaining: int
    status: PaperOrderStatus = PaperOrderStatus.PENDING
    filled_at: pd.Timestamp | None = None
    fill_price: float | None = None
    entry_fee: float = 0.0
    closed_at: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


@dataclass
class PaperPosition:
    order_id: str
    symbol: str
    timeframe: str
    signal: Signal
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    position_size: float
    risk_amount: float
    entry_fee: float
    score: int
    regime: str
    strategy: str


@dataclass(frozen=True)
class PaperTrade:
    order_id: str
    symbol: str
    timeframe: str
    signal: Signal
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    position_size: float
    gross_pnl: float
    fees: float
    net_pnl: float
    risk_amount: float
    r_multiple: float
    exit_reason: str
    score: int
    regime: str
    strategy: str


@dataclass(frozen=True)
class PaperAccountSnapshot:
    starting_equity: float
    equity: float
    realized_net_pnl: float
    unrealized_pnl: float
    fees_paid: float
    open_positions: int
    pending_orders: int
    reserved_risk: float
