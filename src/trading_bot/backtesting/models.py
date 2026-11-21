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
    strategy: str = "baseline"
    score: int = 0
    regime: str = ""
    risk_amount: float = 0.0
    r_multiple: float = 0.0
    tp1_hit: bool = False
    gross_pnl: float = 0.0
    entry_fee: float = 0.0
    exit_fees: float = 0.0
    gross_r: float = 0.0
    fees_r: float = 0.0
    raw_gross_pnl: float = 0.0
    slippage_cost: float = 0.0
    slippage_r: float = 0.0


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
