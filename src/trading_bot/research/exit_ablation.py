"""Reproducible, fixed-parameter exit ablation; never selects the best variant."""
from dataclasses import replace

import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.planning import TradePlanConfig


EXIT_VARIANTS = {
    "full_2r": {"take_profit_1_fraction": 0.0, "move_stop_to_break_even_after_tp1": False},
    "partial_no_be": {"take_profit_1_fraction": 0.5, "move_stop_to_break_even_after_tp1": False},
    "partial_be": {"take_profit_1_fraction": 0.5, "move_stop_to_break_even_after_tp1": True},
}


def run_exit_ablation(data: pd.DataFrame, *, config: TradePlanConfig | None = None, **kwargs):
    """Return raw diagnostics and individual results for all predeclared exits.

    Important: the different exit designs change occupancy, position sizing,
    fees and subsequent entry timing. They are NOT paired observations.
    The best in-sample variant is not a validated strategy.
    """
    base = config or TradePlanConfig()
    rows = []
    results = {}
    if "trade_plan_config" in kwargs:
        raise ValueError("Pass config rather than trade_plan_config.")
    for name, overrides in EXIT_VARIANTS.items():
        result = run_decision_backtest(
            data.copy(),
            trade_plan_config=replace(base, **overrides),
            **kwargs,
        )
        trades = result.trades
        wins = trades.loc[trades["pnl"] > 0, "pnl"] if not trades.empty else pd.Series(dtype=float)
        losses = trades.loc[trades["pnl"] < 0, "pnl"] if not trades.empty else pd.Series(dtype=float)
        rows.append({
            "exit_variant": name,
            "trades": result.total_trades,
            "return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "profit_factor": result.profit_factor,
            "win_rate_pct": result.win_rate_pct,
            "average_net_r": float(trades["r_multiple"].mean()) if not trades.empty else 0.0,
            "average_win": float(wins.mean()) if not wins.empty else 0.0,
            "average_loss": float(losses.mean()) if not losses.empty else 0.0,
            "tp1_count": int(trades["tp1_hit"].sum()) if not trades.empty else 0,
            "stop_count": int(trades["exit_reason"].str.contains("STOP_LOSS").sum()) if not trades.empty else 0,
        })
        results[name] = result
    return pd.DataFrame(rows), results
