"""Local research and dry-run assisted-trading API.

No authenticated exchange access, private keys, or live order placement.
Bind locally (127.0.0.1); this development API has no user authentication.
"""
from dataclasses import asdict, is_dataclass
from enum import Enum
import logging
import math
import os
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
import pandas as pd
from pydantic import BaseModel, Field

from trading_bot.assisted import (
    AssistedTradingService,
    IntentStatus,
    IntentStore,
)
from trading_bot.backtesting import run_decision_backtest
from trading_bot.execution import (
    LiveExecutionGuard,
    ManagedDryRunBrokerAdapter,
    OrderStore,
)
from trading_bot.data import (
    HistoricalDataStore,
    get_ohlcv,
    validate_ohlcv,
)
from trading_bot.paper import PaperJournal
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)
from trading_bot.strategies import normalize_strategy_names

log = logging.getLogger(__name__)
app = FastAPI(title="Trading Bot — Research & Assisted Dashboard", version="0.16.0")

EXCHANGES = {"kraken", "kucoin", "binance", "bybit", "okx", "coinbase"}
MARKET_TYPES = {"spot", "swap", "future"}
TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
SYMBOL_RE = re.compile(r"^[A-Z0-9._-]+/[A-Z0-9._-]+(?::[A-Z0-9._-]+)?$")
PAGE = Path(__file__).with_name("index.html")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _project_path(
    environment_variable: str,
    default_relative: str,
) -> Path:
    raw = os.getenv(environment_variable)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
    return (PROJECT_ROOT / default_relative).resolve()


HISTORY_ROOT = _project_path(
    "TRADING_HISTORY_ROOT",
    "data/history",
)
PAPER_JOURNAL_PATH = Path(
    os.getenv(
        "TRADING_PAPER_JOURNAL",
        "data/paper/continuous_paper.db",
    )
)
ASSISTED_DB_PATH = Path(
    os.getenv(
        "TRADING_ASSISTED_DB",
        "data/assisted/assisted.db",
    )
)
ORDER_DB_PATH = Path(
    os.getenv(
        "TRADING_ORDER_DB",
        "data/orders/orders.db",
    )
)
LIVE_ORDER_DB_PATH = Path(
    os.getenv(
        "TRADING_LIVE_ORDER_DB",
        "data/orders/live_orders.db",
    )
)


class StageIntentRequest(BaseModel):
    exchange: str = "kraken"
    market_type: str = "spot"
    symbol: str
    timeframe: str = "1h"
    confirm_timeframes: list[str] = Field(default_factory=list)
    entry_timeframe: str | None = None
    limit: int = 500
    equity: float = 10_000
    risk_percent: float = 1.0
    min_score: int = 70
    strategies: list[str] = Field(
        default_factory=lambda: ["trend_continuation"]
    )


class IntentDecisionRequest(BaseModel):
    actor: str
    note: str = ""


def _safe(value):
    """Normalize dataclasses, enums, NaN and pandas scalars for strict JSON."""
    if is_dataclass(value) and not isinstance(value, type):
        return _safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return value


def _validate_exchange(exchange: str, market_type: str, timeframe: str):
    if exchange not in EXCHANGES:
        raise HTTPException(422, "Unsupported exchange for this dashboard.")
    if market_type not in MARKET_TYPES:
        raise HTTPException(422, "Unsupported market type.")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(422, "Unsupported timeframe.")


def _validate_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(422, "Use a CCXT symbol such as BTC/USDT or BTC/USDT:USDT.")
    return symbol


def _parse_symbols(raw: str) -> list[str]:
    symbols = [_validate_symbol(piece) for piece in raw.split(",")]
    if not 1 <= len(symbols) <= 8 or len(set(symbols)) != len(symbols):
        raise HTTPException(422, "Provide 1–8 different symbols.")
    return symbols


def _parse_quotes(raw: str) -> tuple[str, ...]:
    values = tuple(
        value.strip().upper()
        for value in raw.split(",")
        if value.strip()
    )
    if not values:
        raise HTTPException(422, "Provide at least one quote currency.")
    if len(set(values)) != len(values):
        raise HTTPException(422, "Quote currencies must be unique.")
    return values


def _parse_strategies(raw: str) -> tuple[str, ...]:
    values = tuple(
        value.strip()
        for value in raw.split(",")
        if value.strip()
    )
    try:
        return normalize_strategy_names(values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _parse_timeframes(raw: str) -> tuple[str, ...]:
    values = tuple(
        value.strip()
        for value in raw.split(",")
        if value.strip()
    )
    if len(set(values)) != len(values):
        raise HTTPException(422, "Confirmation timeframes must be unique.")
    unsupported = [
        value for value in values
        if value not in TIMEFRAMES
    ]
    if unsupported:
        raise HTTPException(
            422,
            "Unsupported confirmation timeframe: "
            + ", ".join(unsupported),
        )
    return values


def _history_store_for_dataset(
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
) -> tuple[HistoricalDataStore, Path, tuple[Path, ...]]:
    roots: list[Path] = [Path(HISTORY_ROOT)]

    # Backward-compatible fallback for datasets that were created relative
    # to the shell working directory by older dashboard/research versions.
    legacy_root = (Path.cwd() / "data" / "history").resolve()
    normalized = {root.resolve() for root in roots}
    if legacy_root not in normalized:
        roots.append(legacy_root)

    checked: list[Path] = []
    for root in roots:
        store = HistoricalDataStore(root)
        path = store.dataset_path(
            exchange,
            market_type,
            symbol,
            timeframe,
        )
        checked.append(path.resolve())
        if path.exists():
            return store, path.resolve(), tuple(checked)

    store = HistoricalDataStore(roots[0])
    path = store.dataset_path(
        exchange,
        market_type,
        symbol,
        timeframe,
    )
    return store, path.resolve(), tuple(checked)


@app.get("/")
def index():
    return FileResponse(PAGE)


@app.get("/api/health")
def health():
    return {"mode": "READ_ONLY", "live_execution": False, "paper_execution": False}




@app.get("/api/paper/status")
def paper_status(
    limit: int = Query(20, ge=1, le=100),
):
    journal = PaperJournal(PAPER_JOURNAL_PATH)
    try:
        active = journal.active_orders()
        all_trades = journal.all_trades()
        trades = journal.trades(limit=limit)
        checkpoints = journal.checkpoints()
        signals = journal.signal_events(limit=limit)
        risk_state = journal.load_risk_state()

        pending = (
            active[active["status"] == "PENDING"]
            if not active.empty
            else active
        )
        filled = (
            active[active["status"] == "FILLED"]
            if not active.empty
            else active
        )
        realized_pnl = (
            float(all_trades["net_pnl"].sum())
            if not all_trades.empty
            else 0.0
        )

        return {
            "journal": str(PAPER_JOURNAL_PATH),
            "metadata": journal.metadata(),
            "summary": {
                "pending_orders": len(pending),
                "open_positions": len(filled),
                "realized_net_pnl": realized_pnl,
            },
            "active_orders": _safe(
                active.to_dict("records")
            ),
            "recent_trades": _safe(
                trades.to_dict("records")
            ),
            "checkpoints": _safe(
                checkpoints.to_dict("records")
            ),
            "recent_signals": _safe(
                signals.to_dict("records")
            ),
            "risk_state": _safe(risk_state),
        }
    finally:
        journal.close()


@app.get("/api/scan")
def scan(
    exchange: str = Query("kraken", max_length=30),
    market_type: str = Query("spot"),
    symbols: str = Query("BTC/USD,ETH/USD", max_length=400),
    timeframe: str = Query("15m"),
    confirm_timeframes: str = Query("", max_length=100),
    entry_timeframe: str = Query("", max_length=20),
    limit: int = Query(500, ge=250, le=1000),
    equity: float = Query(10000.0, gt=0, le=100_000_000),
    risk_percent: float = Query(1.0, gt=0, le=5),
    min_score: int = Query(70, ge=0, le=100),
    strategies: str = Query("trend_continuation", max_length=200),
    auto_universe: bool = Query(False),
    quote_currencies: str = Query("USDT", max_length=100),
    max_markets: int = Query(20, ge=1, le=50),
    min_quote_volume: float = Query(1_000_000, ge=0),
    max_spread_bps: float | None = Query(None, gt=0, le=10_000),
    min_history_candles: int = Query(220, ge=0, le=5000),
    max_history_probes: int = Query(60, ge=1, le=500),
):
    _validate_exchange(exchange, market_type, timeframe)
    confirmation_timeframes = _parse_timeframes(confirm_timeframes)
    entry_timeframe = entry_timeframe.strip()
    if entry_timeframe and entry_timeframe not in TIMEFRAMES:
        raise HTTPException(422, "Unsupported entry timeframe.")
    strategy_names = _parse_strategies(strategies)
    universe_selection = None

    if auto_universe:
        quotes = _parse_quotes(quote_currencies)
        try:
            universe_selection = DynamicMarketUniverse(
                exchange_id=exchange,
                market_type=market_type,
            ).select(
                UniverseConfig(
                    market_type=market_type,
                    quote_currencies=quotes,
                    max_markets=max_markets,
                    min_quote_volume=min_quote_volume,
                    max_spread_bps=max_spread_bps,
                    min_history_candles=min_history_candles,
                    history_timeframe=timeframe,
                    max_history_probes=max_history_probes,
                )
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            log.exception("Universe discovery failure")
            raise HTTPException(
                502,
                "Could not discover a liquid market universe.",
            ) from exc

        markets = universe_selection.symbols
        if not markets:
            raise HTTPException(
                422,
                "No markets matched the universe filters.",
            )
    else:
        markets = _parse_symbols(symbols)

    try:
        scanner = MarketScanner(
            exchange_id=exchange,
            market_type=market_type,
            timeframe=timeframe,
            confirmation_timeframes=confirmation_timeframes,
            entry_timeframe=entry_timeframe or None,
            candle_limit=limit,
            account_equity=equity,
            risk_percent=risk_percent / 100,
            min_confirmed_score=min_score,
            strategy_names=strategy_names,
        )
        results = scanner.scan(markets)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        log.exception("Scanner failure")
        raise HTTPException(502, "Could not scan requested markets.")
    output = []
    for result in results:
        if result.error:
            log.warning("Scanner failed for %s: %s", result.symbol, result.error)
        output.append({
            "symbol": result.symbol,
            "status": result.assessment.status.value if result.assessment else "ERROR",
            "signal": result.assessment.signal.value if result.assessment else "NO_TRADE",
            "score": result.score,
            "strategy": (
                getattr(
                    result.assessment,
                    "strategy",
                    "trend_continuation",
                )
                if result.assessment
                else None
            ),
            "reason": result.assessment.reason if result.assessment else None,
            "structure": _safe(result.structure),
            "setup": _safe(result.setup),
            "data_quality": _safe(result.data_quality),
            "primary_assessment": _safe(
                getattr(result, "primary_assessment", None)
            ),
            "confirmations": _safe(
                getattr(result, "confirmations", ())
            ),
            "multi_timeframe_aligned": getattr(
                result,
                "multi_timeframe_aligned",
                None,
            ),
            "entry_timeframe": getattr(
                result,
                "entry_timeframe",
                None,
            ),
            "entry_trigger": _safe(
                getattr(result, "entry_trigger", None)
            ),
            "candidates": _safe(
                getattr(result, "candidates", ())
            ),
            # Errors may contain upstream request details; do not expose them.
            "error": "Market data or analysis unavailable." if result.error else None,
        })
    return {
        "exchange": exchange,
        "market_type": market_type,
        "timeframe": timeframe,
        "confirmation_timeframes": list(confirmation_timeframes),
        "entry_timeframe": entry_timeframe or None,
        "strategies": list(strategy_names),
        "auto_universe": auto_universe,
        "universe": _safe(universe_selection),
        "results": output,
        "live_execution": False,
    }




@app.get("/api/assisted/intents")
def assisted_intents(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    store = IntentStore(ASSISTED_DB_PATH)
    try:
        statuses = None
        if status:
            try:
                statuses = (IntentStatus(status.upper()),)
            except ValueError as exc:
                raise HTTPException(
                    422,
                    "Unsupported assisted intent status.",
                ) from exc

        intents = store.list(
            statuses=statuses,
            limit=limit,
        )
        return {
            "mode": "DRY_RUN",
            "live_execution": False,
            "database": str(ASSISTED_DB_PATH),
            "intents": _safe(intents),
            "decisions": _safe(
                store.decisions(limit=limit)
            ),
            "receipts": _safe(
                store.execution_receipts(limit=limit)
            ),
        }
    finally:
        store.close()




@app.post("/api/assisted/stage")
def assisted_stage(request: StageIntentRequest):
    _validate_exchange(
        request.exchange,
        request.market_type,
        request.timeframe,
    )
    symbol = _validate_symbol(request.symbol)

    if not 250 <= request.limit <= 1000:
        raise HTTPException(422, "limit must be between 250 and 1000.")
    if not 0 < request.equity <= 100_000_000:
        raise HTTPException(422, "equity must be positive.")
    if not 0 < request.risk_percent <= 5:
        raise HTTPException(
            422,
            "risk_percent must be greater than 0 and <= 5.",
        )
    if not 0 <= request.min_score <= 100:
        raise HTTPException(
            422,
            "min_score must be between 0 and 100.",
        )

    confirmation_timeframes = tuple(
        value.strip()
        for value in request.confirm_timeframes
        if value.strip()
    )
    unsupported = [
        value
        for value in confirmation_timeframes
        if value not in TIMEFRAMES
    ]
    if unsupported:
        raise HTTPException(
            422,
            "Unsupported confirmation timeframe: "
            + ", ".join(unsupported),
        )

    entry_timeframe = (
        request.entry_timeframe.strip()
        if request.entry_timeframe
        else None
    )
    if entry_timeframe and entry_timeframe not in TIMEFRAMES:
        raise HTTPException(422, "Unsupported entry timeframe.")

    try:
        strategy_names = normalize_strategy_names(
            tuple(request.strategies)
        )
        scanner = MarketScanner(
            exchange_id=request.exchange,
            market_type=request.market_type,
            timeframe=request.timeframe,
            confirmation_timeframes=confirmation_timeframes,
            entry_timeframe=entry_timeframe,
            candle_limit=request.limit,
            account_equity=request.equity,
            risk_percent=request.risk_percent / 100,
            min_confirmed_score=request.min_score,
            strategy_names=strategy_names,
        )
        result = scanner.scan_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        log.exception("Assisted staging scan failed")
        raise HTTPException(
            502,
            "Could not evaluate the requested market.",
        ) from exc

    store = IntentStore(ASSISTED_DB_PATH)
    try:
        service = AssistedTradingService(store)
        try:
            intent = service.build_intent(result)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        created = store.queue(intent)
        if not created:
            existing = store.get_by_fingerprint(
                intent.fingerprint
            )
            if existing is None:
                raise HTTPException(
                    409,
                    "Matching intent already exists.",
                )
            intent = existing

        return {
            "created": created,
            "mode": "DRY_RUN",
            "live_execution": False,
            "intent": _safe(intent),
        }
    finally:
        store.close()




@app.post("/api/assisted/{intent_id}/approve")
def assisted_approve(
    intent_id: str,
    request: IntentDecisionRequest,
):
    if not request.actor.strip():
        raise HTTPException(422, "Decision actor is required.")

    store = IntentStore(ASSISTED_DB_PATH)
    order_store = OrderStore(ORDER_DB_PATH)
    try:
        broker = ManagedDryRunBrokerAdapter(
            order_store
        )
        service = AssistedTradingService(
            store,
            broker=broker,
        )
        try:
            intent, receipt = service.approve(
                intent_id,
                actor=request.actor,
                note=request.note,
                execute=True,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        return {
            "mode": "MANAGED_DRY_RUN",
            "live_execution": False,
            "intent": _safe(intent),
            "receipt": _safe(receipt),
        }
    finally:
        order_store.close()
        store.close()


@app.post("/api/assisted/{intent_id}/reject")
def assisted_reject(
    intent_id: str,
    request: IntentDecisionRequest,
):
    if not request.actor.strip():
        raise HTTPException(422, "Decision actor is required.")

    store = IntentStore(ASSISTED_DB_PATH)
    try:
        service = AssistedTradingService(store)
        try:
            intent = service.reject(
                intent_id,
                actor=request.actor,
                note=request.note,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        return {
            "mode": "DRY_RUN",
            "live_execution": False,
            "intent": _safe(intent),
        }
    finally:
        store.close()




@app.get("/api/execution/status")
def execution_status():
    store = OrderStore(LIVE_ORDER_DB_PATH)
    try:
        guard = LiveExecutionGuard(store)
        state = guard.status()
        return {
            "mode": "LIVE_OPERATOR_CLI_ONLY",
            "web_live_submission": False,
            "database": str(LIVE_ORDER_DB_PATH),
            "environment_enabled": (
                guard.environment_enabled
            ),
            "context": _safe(
                store.load_execution_context()
            ),
            "control": _safe(state),
            "active_orders": _safe(
                store.active_orders()
            ),
        }
    finally:
        store.close()


@app.get("/api/orders/status")
def order_status(
    limit: int = Query(50, ge=1, le=200),
):
    store = OrderStore(ORDER_DB_PATH)
    try:
        orders = store.list_orders(limit=limit)
        return {
            "mode": "MANAGED_DRY_RUN",
            "live_execution": False,
            "database": str(ORDER_DB_PATH),
            "orders": _safe(orders),
        }
    finally:
        store.close()


@app.get("/api/backtest")
def backtest(
    exchange: str = Query("kraken"),
    market_type: str = Query("spot"),
    symbol: str = Query("BTC/USD", max_length=50),
    timeframe: str = Query("1h"),
    data_source: str = Query("local", pattern="^(local|live)$"),
    start: str | None = Query(None, max_length=32),
    end: str | None = Query(None, max_length=32),
    limit: int = Query(720, ge=250, le=1000),
    equity: float = Query(10000.0, gt=0, le=100_000_000),
    risk_percent: float = Query(1.0, gt=0, le=5),
    min_score: int = Query(70, ge=0, le=100),
    strategies: str = Query("trend_continuation", max_length=200),
    fee_rate: float = Query(0.001, ge=0, le=0.05),
    slippage_bps: float = Query(5.0, ge=0, le=500),
):
    _validate_exchange(exchange, market_type, timeframe)
    symbol = _validate_symbol(symbol)
    strategy_names = _parse_strategies(strategies)

    try:
        if data_source == "local":
            store, dataset_path, checked_paths = (
                _history_store_for_dataset(
                    exchange,
                    market_type,
                    symbol,
                    timeframe,
                )
            )
            candles = store.load(
                exchange,
                market_type,
                symbol,
                timeframe,
            )
            if candles.empty:
                checked = " | ".join(
                    str(path)
                    for path in checked_paths
                )
                raise HTTPException(
                    404,
                    "No saved historical dataset matches "
                    f"{exchange} · {market_type} · {symbol} · {timeframe}. "
                    f"Checked: {checked}",
                )

            filtered = candles.copy()
            filtered["timestamp"] = pd.to_datetime(
                filtered["timestamp"],
                utc=True,
            )

            if start:
                start_ts = pd.Timestamp(start)
                start_ts = (
                    start_ts.tz_localize("UTC")
                    if start_ts.tzinfo is None
                    else start_ts.tz_convert("UTC")
                )
                filtered = filtered[
                    filtered["timestamp"] >= start_ts
                ]

            if end:
                end_ts = pd.Timestamp(end)
                end_ts = (
                    end_ts.tz_localize("UTC")
                    if end_ts.tzinfo is None
                    else end_ts.tz_convert("UTC")
                )
                # A date-only end value includes the full selected day.
                if len(end) == 10:
                    end_ts = end_ts + pd.Timedelta(days=1)
                filtered = filtered[
                    filtered["timestamp"] < end_ts
                ]

            closed = filtered.reset_index(drop=True)
            source_path = str(dataset_path)
        else:
            candles = get_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                limit=limit,
                exchange_id=exchange,
                market_type=market_type,
            )
            # Conservatively omit the still-open exchange candle.
            closed = candles.iloc[:-1].copy()
            source_path = None

        quality = validate_ohlcv(
            closed,
            timeframe=timeframe,
            min_rows=220,
        )
        if not quality.valid:
            raise HTTPException(
                422,
                "Not enough valid closed candles for backtesting.",
            )

        result = run_decision_backtest(
            closed,
            initial_equity=equity,
            risk_percent=risk_percent / 100,
            min_confirmed_score=min_score,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            allow_short=market_type != "spot",
            strategy_names=strategy_names,
        )
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            422,
            f"Invalid backtest selection: {exc}",
        ) from exc
    except Exception:
        log.exception("Backtest failed")
        raise HTTPException(
            502,
            "Market data or backtesting failed.",
        )

    data_start = (
        closed["timestamp"].min()
        if not closed.empty
        else None
    )
    data_end = (
        closed["timestamp"].max()
        if not closed.empty
        else None
    )

    return {
        "symbol": symbol,
        "exchange": exchange,
        "market_type": market_type,
        "timeframe": timeframe,
        "data_source": data_source,
        "source_path": source_path,
        "data_rows": len(closed),
        "data_start": _safe(data_start),
        "data_end": _safe(data_end),
        "strategies": list(strategy_names),
        "assumptions": {
            "fee_rate_per_side": fee_rate,
            "slippage_bps_per_side": slippage_bps,
            "min_score": min_score,
            "single_timeframe_only": True,
        },
        "metrics": _safe({
            "initial_equity": result.initial_equity,
            "final_equity": result.final_equity,
            "return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_trades": result.total_trades,
            "win_rate_pct": result.win_rate_pct,
            "profit_factor": result.profit_factor,
            "average_pnl": result.average_pnl,
        }),
        "recent_trades": _safe(
            result.trades.tail(10).to_dict("records")
        ),
        "warning": (
            "This historical backtest is single-timeframe. "
            "Scanner higher-timeframe confirmation and lower-timeframe "
            "entry-trigger filters are not applied by this endpoint yet."
        ),
    }

