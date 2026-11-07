from dataclasses import dataclass

import pandas as pd


@dataclass
class CompletedTrade:
    signal: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    pnl: float
    exit_reason: str


@dataclass
class BacktestResult:
    initial_equity: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    total_trades: int
    win_rate_pct: float
    profit_factor: float
    average_pnl: float
    trades: pd.DataFrame
    signals_seen: int = 0
    entries_skipped: int = 0
