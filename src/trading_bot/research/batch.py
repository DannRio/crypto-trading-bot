import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.data import HistoricalDataStore, validate_ohlcv
from trading_bot.research.models import DatasetSpec


def _side_count(trades: pd.DataFrame, side: str) -> int:
    if trades.empty or "signal" not in trades.columns:
        return 0
    return int((trades["signal"] == side).sum())


def run_batch_research(
    specs: list[DatasetSpec],
    store: HistoricalDataStore | None = None,
    initial_equity: float = 10_000,
    risk_percent: float = 0.01,
    min_confirmed_score: int = 70,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    allow_short: bool | None = None,
    min_history_days: float = 0,
) -> tuple[pd.DataFrame, dict[tuple[str, str, str, str], object]]:
    store = store or HistoricalDataStore()
    summary_rows: list[dict] = []
    details: dict[tuple[str, str, str, str], object] = {}

    for spec in specs:
        data = store.load(
            spec.exchange_id,
            spec.market_type,
            spec.symbol,
            spec.timeframe,
        )

        row = {
            "exchange": spec.exchange_id,
            "market_type": spec.market_type,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
            "rows": len(data),
        }

        quality = validate_ohlcv(
            data,
            timeframe=spec.timeframe,
            min_rows=220,
        )
        if not quality.valid:
            row.update(
                {
                    "status": "INVALID_DATA",
                    "error": "; ".join(quality.issues),
                }
            )
            summary_rows.append(row)
            continue

        span_days = (
            (data["timestamp"].max() - data["timestamp"].min()).total_seconds()
            / 86_400
        )
        row["span_days"] = span_days
        row["missing_intervals"] = quality.missing_intervals
        if span_days < min_history_days:
            row.update({
                "status": "INSUFFICIENT_HISTORY",
                "error": (
                    f"Only {span_days:.1f} days available; "
                    f"at least {min_history_days:.1f} required."
                ),
            })
            summary_rows.append(row)
            continue

        result = run_decision_backtest(
            data=data,
            initial_equity=initial_equity,
            risk_percent=risk_percent,
            min_confirmed_score=min_confirmed_score,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            allow_short=(
                spec.market_type != "spot"
                and (allow_short is None or allow_short)
            ),
        )

        trades = result.trades
        average_r = (
            float(trades["r_multiple"].mean())
            if not trades.empty and "r_multiple" in trades.columns
            else 0.0
        )
        median_score = (
            float(trades["score"].median())
            if not trades.empty and "score" in trades.columns
            else 0.0
        )

        row.update(
            {
                "status": "GAPS_PRESENT" if quality.missing_intervals else "OK",
                "start": data["timestamp"].min(),
                "end": data["timestamp"].max(),
                "trades": result.total_trades,
                "long_trades": _side_count(trades, "LONG"),
                "short_trades": _side_count(trades, "SHORT"),
                "return_pct": result.total_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "win_rate_pct": result.win_rate_pct,
                "profit_factor": result.profit_factor,
                "average_pnl": result.average_pnl,
                "average_r": average_r,
                "median_score": median_score,
                "error": "",
            }
        )
        summary_rows.append(row)

        key = (
            spec.exchange_id,
            spec.market_type,
            spec.symbol,
            spec.timeframe,
        )
        details[key] = result

    return pd.DataFrame(summary_rows), details
