"""Read-only trading-strategy diagnostics built from one canonical backtest.

Annual returns use continuously marked portfolio equity (including open P&L);
trade buckets use realized net P&L and are grouped by EXIT year. A bucket's
net P&L is not a standalone deployable strategy return.
"""
from __future__ import annotations

from collections import defaultdict
import math

import pandas as pd

from trading_bot.backtesting.models import BacktestResult


SCORE_BUCKETS = ("0–69", "70–79", "80–89", "90–100")


def score_bucket(value: int | float) -> str:
    if value < 70:
        return "0–69"
    if value < 80:
        return "70–79"
    if value < 90:
        return "80–89"
    return "90–100"


def _finite(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _trade_stats(trades: list[dict]) -> dict:
    count = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    gross_profit = float(sum(wins))
    gross_loss = abs(float(sum(losses)))
    r_values = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    holding = [t["holding_hours"] for t in trades if t["holding_hours"] is not None]
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / count * 100) if count else None,
        "net_pnl": float(sum(t["pnl"] for t in trades)),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "average_pnl": (sum(t["pnl"] for t in trades) / count) if count else None,
        "average_r": (sum(r_values) / len(r_values)) if r_values else None,
        "average_holding_hours": (sum(holding) / len(holding)) if holding else None,
    }


def _normalize_trades(raw: pd.DataFrame) -> list[dict]:
    if raw is None or raw.empty:
        return []
    records = []
    for index, trade in enumerate(raw.to_dict("records"), start=1):
        entry = pd.Timestamp(trade["entry_time"])
        exit_time = pd.Timestamp(trade["exit_time"])
        if entry.tz is None:
            entry = entry.tz_localize("UTC")
        else:
            entry = entry.tz_convert("UTC")
        if exit_time.tz is None:
            exit_time = exit_time.tz_localize("UTC")
        else:
            exit_time = exit_time.tz_convert("UTC")
        if exit_time < entry:
            raise ValueError("Trade exit cannot precede its entry.")
        score = int(trade.get("score", 0))
        if not 0 <= score <= 100:
            raise ValueError("Trade score outside 0–100.")
        holding_hours = (exit_time - entry).total_seconds() / 3600
        rec = {
            "id": index,
            "entry_time": entry.isoformat(),
            "exit_time": exit_time.isoformat(),
            "year": int(exit_time.year),
            "side": str(trade["signal"]),
            "score": score,
            "score_bucket": score_bucket(score),
            "regime": str(trade.get("regime", "") or "UNKNOWN"),
            "exit_reason": str(trade["exit_reason"]),
            "entry_price": _finite(trade["entry_price"]),
            "exit_price": _finite(trade["exit_price"]),
            "stop_loss": _finite(trade["stop_loss"]),
            "take_profit": _finite(trade["take_profit"]),
            "quantity": _finite(trade["position_size"]),
            "pnl": _finite(trade["pnl"]),
            "r_multiple": _finite(trade.get("r_multiple")),
            "risk_amount": _finite(trade.get("risk_amount")),
            "holding_hours": holding_hours,
        }
        if rec["pnl"] is None:
            raise ValueError("Nonfinite trade P&L.")
        records.append(rec)
    # Keep consistent chronological reporting even if caller data was unsorted.
    return sorted(records, key=lambda t: (t["exit_time"], t["entry_time"], t["id"]))


def _marked_curve(result: BacktestResult) -> pd.DataFrame:
    raw = result.equity_curve
    if raw is None or raw.empty:
        raise ValueError("Backtest must supply a candle-close marked equity curve.")
    frame = raw[["timestamp", "equity"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["equity"] = pd.to_numeric(frame["equity"], errors="raise")
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if frame["timestamp"].duplicated().any() or not frame["equity"].map(math.isfinite).all():
        raise ValueError("Equity curve has duplicate timestamps or nonfinite values.")
    if not math.isclose(float(frame.iloc[-1]["equity"]), float(result.final_equity), rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError("Final marked equity must equal final backtest equity.")
    peak = frame["equity"].cummax().clip(lower=0.0)
    frame["drawdown_pct"] = (
        (peak - frame["equity"]) / peak.where(peak > 0, other=math.nan) * 100
    ).fillna(0)
    return frame


def _downsample(frame: pd.DataFrame, max_points: int = 1200) -> list[dict]:
    """Retain first/last and bucket extrema, not naive data thinning."""
    if max_points < 4:
        raise ValueError("At least four curve points are required.")
    length = len(frame)
    if length <= max_points:
        indices = list(range(length))
    else:
        inner = length - 2
        buckets = max(1, (max_points - 2) // 2)
        width = math.ceil(inner / buckets)
        chosen = {0, length - 1}
        for start in range(1, length - 1, width):
            stop = min(length - 1, start + width)
            part = frame.iloc[start:stop]
            chosen.add(int(part["equity"].idxmin()))
            chosen.add(int(part["equity"].idxmax()))
        indices = sorted(chosen)
    return [
        {
            "time": frame.iloc[i]["timestamp"].isoformat(),
            "equity": float(frame.iloc[i]["equity"]),
            "drawdown_pct": float(frame.iloc[i]["drawdown_pct"]),
        }
        for i in indices
    ]


def _annual_equity(frame: pd.DataFrame, initial_equity: float, trades: list[dict]) -> list[dict]:
    prior = float(initial_equity)
    output = []
    by_year: dict[int, list[dict]] = defaultdict(list)
    for trade in trades:
        by_year[trade["year"]].append(trade)
    for year, group in frame.groupby(frame["timestamp"].dt.year, sort=True):
        final = float(group.iloc[-1]["equity"])
        start = prior
        yearly_max_dd = float(group["drawdown_pct"].max())
        stats = _trade_stats(by_year.get(int(year), []))
        output.append({
            "year": int(year),
            "start": group.iloc[0]["timestamp"].isoformat(),
            "end": group.iloc[-1]["timestamp"].isoformat(),
            "start_equity": start,
            "end_equity": final,
            "return_pct": ((final / start - 1) * 100) if start > 0 else None,
            "max_global_peak_drawdown_pct": yearly_max_dd,
            "trades_closed": stats["trades"],
            "closed_trade_pnl": stats["net_pnl"],
            "win_rate_pct": stats["win_rate_pct"],
            "profit_factor": stats["profit_factor"],
        })
        prior = final
    return output


def _group_stats(trades: list[dict], field: str, categories: tuple | None = None) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade[field])].append(trade)
    ordered = list(categories) if categories is not None else sorted(grouped)
    return [{"label": label, **_trade_stats(grouped.get(label, []))}
            for label in ordered]


def build_diagnostics(result: BacktestResult, max_equity_points: int = 1200) -> dict:
    """Build client-friendly aggregate data; no reoptimization or lookahead."""
    curve = _marked_curve(result)
    trades = _normalize_trades(result.trades)
    first = curve.iloc[0]
    last = curve.iloc[-1]
    return {
        "strategy": "trend_continuation",
        "period": {
            "start": first["timestamp"].isoformat(),
            "end": last["timestamp"].isoformat(),
            "candles_with_equity": len(curve),
        },
        "summary": {
            "initial_equity": float(result.initial_equity),
            "final_equity": float(result.final_equity),
            "return_pct": float(result.total_return_pct),
            "max_drawdown_pct": float(result.max_drawdown_pct),
            **_trade_stats(trades),
        },
        "yearly": _annual_equity(curve, result.initial_equity, trades),
        "score_buckets": _group_stats(trades, "score_bucket", SCORE_BUCKETS),
        "regimes": _group_stats(trades, "regime"),
        "directions": _group_stats(trades, "side"),
        "exit_reasons": _group_stats(trades, "exit_reason"),
        "equity_curve": _downsample(curve, max_equity_points),
        "trades": trades,
        "disclaimers": [
            "Equity curve marks open positions at candle close, minus an estimated closing fee.",
            "Annual returns use marked equity at year boundaries; trade year means EXIT year.",
            "Score/regime bucket P&L is descriptive, not an independently executable strategy return.",
            "Drawdowns use global peak-to-trough equity; the yearly drawdown column is the worst global-peak drawdown observed within that year.",
            "OHLC simulation omits contract multipliers, funding, liquidation and intrabar tick ordering.",
            "Historical performance and diagnostics do not establish future profitability.",
        ],
    }
