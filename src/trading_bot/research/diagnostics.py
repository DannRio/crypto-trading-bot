"""Trade-level research attribution. All outputs are descriptive, never fitted."""
import pandas as pd


def trade_diagnostics(trades: pd.DataFrame, initial_equity: float) -> dict:
    if trades.empty:
        return {
            "by_strategy": [], "by_regime": [], "by_score_bucket": [],
            "by_exit_reason": [], "by_year": [], "equity_curve": [],
            "gross_total": 0.0, "fees_total": 0.0,
            "gross_average_r": 0.0, "fees_average_r": 0.0,
        }
    df = trades.copy().sort_values("exit_time").reset_index(drop=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    df["year"] = df["exit_time"].dt.year
    df["score_bucket"] = pd.cut(
        df["score"], bins=[-1, 49, 59, 69, 79, 89, 100],
        labels=["0-49", "50-59", "60-69", "70-79", "80-89", "90-100"],
    ).astype(str)
    df["cumulative_equity"] = initial_equity + df["pnl"].cumsum()

    def aggregate(key):
        result = []
        for group, rows in df.groupby(key, dropna=False, observed=True, sort=True):
            wins = rows[rows["pnl"] > 0]["pnl"].sum()
            losses = abs(rows[rows["pnl"] < 0]["pnl"].sum())
            result.append({
                key: str(group),
                "trades": int(len(rows)),
                "net_pnl": float(rows["pnl"].sum()),
                "average_net_r": float(rows["r_multiple"].mean()),
                "win_rate_pct": float((rows["pnl"] > 0).mean() * 100),
                "profit_factor": float(wins / losses) if losses else (None if wins else 0.0),
            })
        return result
    gross = float(df["gross_pnl"].sum()) if "gross_pnl" in df else None
    fees = float(df[["entry_fee", "exit_fees"]].sum().sum()) if {"entry_fee","exit_fees"}.issubset(df) else None
    return {
        "by_strategy": aggregate("strategy"),
        "by_regime": aggregate("regime"),
        "by_score_bucket": aggregate("score_bucket"),
        "by_exit_reason": aggregate("exit_reason"),
        "by_year": aggregate("year"),
        "equity_curve": [
            {"exit_time": t.isoformat(), "equity": float(e)}
            for t, e in zip(df["exit_time"], df["cumulative_equity"])
        ],
        "gross_total": gross,
        "fees_total": fees,
        "gross_average_r": float(df["gross_r"].mean()) if "gross_r" in df else None,
        "fees_average_r": float(df["fees_r"].mean()) if "fees_r" in df else None,
    }
