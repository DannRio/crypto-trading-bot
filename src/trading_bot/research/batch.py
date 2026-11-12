import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.data import HistoricalDataStore, validate_ohlcv
from trading_bot.research.models import DatasetSpec
from trading_bot.strategies import DEFAULT_STRATEGIES


class ResearchDataError(ValueError):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


def dataset_identity(spec: DatasetSpec) -> dict:
    return {"exchange": spec.exchange_id, "market_type": spec.market_type,
            "symbol": spec.symbol, "timeframe": spec.timeframe}


def load_research_inputs(
    spec: DatasetSpec,
    store: HistoricalDataStore,
    confirmation_timeframes: tuple[str, ...] = (),
    entry_timeframe: str | None = None,
    execution_timeframe: str | None = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Load every requested timeframe, rejecting incomplete per-asset input sets."""
    timeframes = tuple(dict.fromkeys((spec.timeframe, *confirmation_timeframes,
                                     *((entry_timeframe,) if entry_timeframe else ()),
                                     *((execution_timeframe,) if execution_timeframe else ()))))
    frames = {}
    for timeframe in timeframes:
        try:
            data = store.load(spec.exchange_id, spec.market_type, spec.symbol, timeframe)
        except (OSError, ValueError, KeyError, pd.errors.ParserError) as exc:
            raise ResearchDataError("DATA_ERROR", f"Could not read {timeframe} dataset: {exc}") from exc
        if data.empty:
            raise ResearchDataError("MISSING_DATA", f"Missing or empty saved {timeframe} dataset.")
        minimum = 220 if timeframe == spec.timeframe or timeframe in confirmation_timeframes or timeframe == entry_timeframe else 1
        quality = validate_ohlcv(data, timeframe=timeframe, min_rows=minimum)
        if not quality.valid or quality.missing_intervals:
            raise ResearchDataError("INVALID_DATA", f"{timeframe}: " + "; ".join(quality.issues + quality.warnings))
        frames[timeframe] = data
    return frames.pop(spec.timeframe), frames


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
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
    confirmation_timeframes: tuple[str, ...] = (),
    entry_timeframe: str | None = None,
    execution_timeframe: str | None = None,
) -> tuple[pd.DataFrame, dict[tuple[str, str, str, str], object]]:
    store = store or HistoricalDataStore()
    summary_rows: list[dict] = []
    details: dict[tuple[str, str, str, str], object] = {}

    for spec in specs:
        row = dict(dataset_identity(spec), rows=0,
                   confirmation_timeframes=",".join(confirmation_timeframes),
                   entry_timeframe=entry_timeframe or "", execution_timeframe=execution_timeframe or "")
        try:
            data, secondary = load_research_inputs(
                spec, store, confirmation_timeframes, entry_timeframe, execution_timeframe)
        except ResearchDataError as exc:
            row.update(status=exc.status, error=str(exc))
            summary_rows.append(row)
            continue
        row["rows"] = len(data)
        try:
            result = run_decision_backtest(
                data=data, primary_timeframe=spec.timeframe,
                initial_equity=initial_equity,
                risk_percent=risk_percent,
                min_confirmed_score=min_confirmed_score,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
                allow_short=spec.market_type != "spot",
                strategy_names=strategy_names,
                confirmation_timeframes=confirmation_timeframes,
                entry_timeframe=entry_timeframe,
                execution_timeframe=execution_timeframe,
                secondary_data=secondary,
            )
        except (ValueError, RuntimeError) as exc:
            row.update(status="BACKTEST_ERROR", error=str(exc))
            summary_rows.append(row)
            continue

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
        strategy_mix = ""
        if not trades.empty and "strategy" in trades.columns:
            counts = trades["strategy"].value_counts()
            strategy_mix = ",".join(
                f"{name}:{int(count)}"
                for name, count in counts.items()
            )

        row.update(
            {
                "status": "OK",
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
                "strategies": ",".join(strategy_names),
                "strategy_mix": strategy_mix,
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
