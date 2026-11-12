"""Local analysis, historical validation, and one controlled paper account.

No authenticated exchange access, private keys, or live order placement.
Bind locally (127.0.0.1); this development API has no user authentication.
"""
from dataclasses import asdict, is_dataclass
from contextlib import asynccontextmanager
from enum import Enum
from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from trading_bot.analysis.service import AnalysisRequest, CoinAnalysisService
from trading_bot.analysis.journal import AnalysisJournal

from trading_bot.backtesting import run_decision_backtest
from trading_bot.config import builtin_profiles, operator_policy, INDICATOR_WINDOW
from trading_bot.paper.runtime import BotSettings, PaperRuntime, RuntimeConflict
from trading_bot.research.jobs import HistoryJobs, HistoryRequest, HistoryJobConflict
from trading_bot.planning import TradePlanConfig
from trading_bot.research.diagnostics import trade_diagnostics
from trading_bot.research.exit_ablation import run_exit_ablation
from trading_bot.research.evaluation import run_out_of_sample_evaluation
from trading_bot.data import (
    HistoricalDataStore,
    get_ohlcv,
    validate_ohlcv,
    timeframe_to_milliseconds,
)
from trading_bot.data.validation import latest_expected_close
from trading_bot.paper import PaperJournal
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)
from trading_bot.strategies import normalize_strategy_names
from trading_bot.scanner.multi_timeframe import validate_confirmation_timeframes, validate_entry_timeframe

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app):
    yield
    if getattr(_bot_runtime, "cache_info", lambda: None)() and _bot_runtime.cache_info().currsize:
        _bot_runtime().shutdown(timeout=15)
    if getattr(_history_jobs, "cache_info", lambda: None)() and _history_jobs.cache_info().currsize:
        _history_jobs().shutdown(timeout=2)


app = FastAPI(title="Trading Bot — Core Paper Release", version="0.19.0", lifespan=lifespan)

EXCHANGES = {"kraken", "kucoin", "binance", "bybit", "okx", "coinbase"}
MARKET_TYPES = {"spot", "swap", "future"}
TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
SYMBOL_RE = re.compile(r"^[A-Z0-9._-]+/[A-Z0-9._-]+(?::[A-Z0-9._-]+)?$")
PAGE = Path(__file__).with_name("index.html")
COIN_PAGE = Path(__file__).with_name("coin.html")
PROJECT_ROOT = Path(os.getenv("TRADING_DATA_DIR", str(Path.cwd() / "data"))).expanduser().resolve().parent
DATA_ROOT = Path(os.getenv("TRADING_DATA_DIR", str(Path.cwd() / "data"))).expanduser().resolve()


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
    str(DATA_ROOT / "history"),
)
PAPER_JOURNAL_PATH = _project_path("TRADING_PAPER_JOURNAL", str(DATA_ROOT / "paper" / "core_paper.db"))






class CoinAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    query: str = Field(min_length=1, max_length=120)
    exchange: Literal["kucoin", "kraken", "binance", "bybit", "okx", "coinbase"] = "kucoin"
    market_type: Literal["spot", "swap"] = "spot"
    quote: Literal["USD", "USDT", "USDC"] = "USDT"
    equity: float = Field(10_000, ge=1, le=1e9)
    risk_percent: float = Field(0.25, ge=0.01, le=5)
    notional: float = Field(1_000, ge=1, le=1e8)
    fee_rate: float = Field(0.001, ge=0, le=0.05)
    slippage_bps: float = Field(5, ge=0, le=500)
    include_fundamentals: bool = False


class BotStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    exchange: Literal["kucoin", "kraken", "binance", "bybit", "okx", "coinbase"] = "kucoin"
    quote: Literal["USD", "USDT", "USDC"] = "USDT"
    symbols: list[str] = Field(default_factory=list, max_length=30)
    max_markets: int = Field(10, ge=1, le=30)
    equity: float = Field(10_000, ge=1, le=1e9)
    risk_percent: float = Field(.25, ge=.01, le=5)
    notional: float = Field(1_000, ge=1, le=1e8)
    fee_rate: float = Field(.001, ge=0, le=.05)
    slippage_bps: float = Field(5, ge=0, le=500)


class HistoryPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    exchange: Literal["kucoin", "kraken", "binance", "bybit", "okx", "coinbase"]
    market_type: Literal["spot", "swap"] = "spot"
    symbol: str = Field(min_length=3, max_length=50)
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@lru_cache(maxsize=1)
def _bot_runtime():
    return PaperRuntime(PAPER_JOURNAL_PATH)


@lru_cache(maxsize=1)
def _history_jobs():
    return HistoryJobs(HISTORY_ROOT)


def _same_origin(request: Request):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(403, "Use the local dashboard origin for this operation.")


@app.get("/api/bot/status")
def bot_status(limit: int = Query(20, ge=1, le=100)):
    runtime = _bot_runtime().status()
    paper = paper_status(limit=limit)
    if runtime.get("active") and runtime.get("owner") == "this_dashboard" and runtime.get("account") is not None:
        paper["account"] = runtime["account"]
    return {"runtime": runtime, "paper": paper,
            "capabilities": {"mode": "paper", "live_orders": False, "market_type": "spot"}}


@app.post("/api/bot/start", status_code=202)
def bot_start(payload: BotStartRequest, request: Request):
    _same_origin(request)
    try:
        settings = BotSettings(**payload.model_dump())
        runtime = _bot_runtime().start(settings)
    except RuntimeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"runtime": runtime, "capabilities": {"mode": "paper", "live_orders": False}}


@app.post("/api/bot/stop", status_code=202)
def bot_stop(request: Request):
    _same_origin(request)
    try:
        runtime = _bot_runtime().stop()
    except RuntimeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"runtime": runtime, "capabilities": {"mode": "paper", "live_orders": False}}


@app.post("/api/history/prepare", status_code=202)
def prepare_history(payload: HistoryPrepareRequest, request: Request):
    _same_origin(request)
    try:
        parameters = HistoryRequest(**payload.model_dump())
        return _history_jobs().start(parameters)
    except HistoryJobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/history/jobs/{job_id}")
def history_job(job_id: str):
    try:
        canonical = str(UUID(job_id))
    except ValueError as exc:
        raise HTTPException(422, "A valid history job ID is required.") from exc
    result = _history_jobs().get(canonical)
    if result is None:
        raise HTTPException(404, "History job not found. Jobs from a previous server session must be prepared again.")
    return result


@lru_cache(maxsize=1)
def _analysis_service():
    path = _project_path("TRADING_ANALYSIS_DB", str(DATA_ROOT / "analysis" / "reports.db"))
    return CoinAnalysisService(journal=AnalysisJournal(path))


@app.exception_handler(RequestValidationError)
async def analysis_validation_error(request, exc):
    if request.url.path in {"/api/analyze", "/api/bot/start", "/api/history/prepare"}:
        # Strict JSON errors must not echo NaN/Infinity or unexpected secret
        # fields supplied by a caller. Preserve existing route behavior.
        return JSONResponse(status_code=422, content={"detail": [
            {key: error[key] for key in ("type", "loc", "msg") if key in error}
            for error in exc.errors()
        ]})
    return await request_validation_exception_handler(request, exc)


@app.get("/analyze")
def coin_analysis_page():
    return FileResponse(COIN_PAGE)


@app.post("/api/analyze")
def analyze_coin(request: CoinAnalysisRequest):
    try:
        parameters = AnalysisRequest(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return _analysis_service().analyze(parameters)
    except Exception as exc:
        log.error("Coin analysis unavailable (%s).", type(exc).__name__)
        raise HTTPException(503, "Analysis or journal persistence is unavailable; no signal was issued.") from exc


@app.get("/api/analysis/history")
def analysis_history(limit: int = Query(25, ge=1, le=500)):
    try:
        return {"reports": _analysis_service().journal.history(limit)}
    except Exception as exc:
        log.error("Analysis history unavailable (%s).", type(exc).__name__)
        raise HTTPException(503, "Verified analysis history is unavailable.") from exc


@app.get("/api/analysis/reports/{report_id}")
def analysis_report(report_id: str):
    try:
        canonical = str(UUID(report_id))
    except ValueError as exc:
        raise HTTPException(422, "A valid analysis report ID is required.") from exc
    try:
        report = _analysis_service().journal.get(canonical)
    except Exception as exc:
        log.error("Analysis report unavailable (%s).", type(exc).__name__)
        raise HTTPException(503, "The saved analysis could not be verified.") from exc
    if report is None:
        raise HTTPException(404, "Analysis report not found.")
    return report


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
    if not 1 <= len(symbols) <= 100 or len(set(symbols)) != len(symbols):
        raise HTTPException(422, "Provide 1–100 different symbols.")
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
    runtime = _bot_runtime().status()
    return {"mode": "PAPER_BOT", "live_execution": False,
            "paper_execution": runtime["active"], "worker_state": runtime["state"],
            "worker_health": runtime["health"], "worker_owner": runtime["owner"]}


@app.get("/api/profiles")
def configuration_profiles():
    return {"profiles": _safe(builtin_profiles()), "live_execution": False}


def _metadata_json(metadata: dict, key: str) -> dict:
    try:
        value = json.loads(metadata.get(key, "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}




@app.get("/api/paper/status")
def paper_status(
    limit: int = Query(20, ge=1, le=100),
):
    if not Path(PAPER_JOURNAL_PATH).is_file():
        return {"status": "not_started", "journal": str(PAPER_JOURNAL_PATH), "metadata": {},
                "universe": {}, "heartbeat": {}, "summary": {"pending_orders": 0,
                "open_positions": 0, "realized_net_pnl": 0}, "active_orders": [],
                "recent_trades": [], "checkpoints": [], "recent_signals": [], "risk_state": None}
    try:
        journal = PaperJournal.open_readonly(PAPER_JOURNAL_PATH)
    except Exception as exc:
        raise HTTPException(503, "The saved paper account is unavailable; no account was created or reset.") from exc
    try:
        active = journal.active_orders()
        all_trades = journal.all_trades()
        trades = journal.trades(limit=limit)
        checkpoints = journal.checkpoints()
        signals = journal.signal_events(limit=limit)
        risk_state = journal.load_risk_state()
        metadata = journal.metadata()

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
            "status": "available",
            "journal": str(PAPER_JOURNAL_PATH),
            "metadata": metadata,
            "universe": _metadata_json(metadata, "universe_status"),
            "heartbeat": _metadata_json(metadata, "heartbeat"),
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
    except Exception as exc:
        raise HTTPException(503, "The saved paper account could not be read; no account was created or reset.") from exc
    finally:
        journal.close()


@app.get("/api/scan")
def scan(
    exchange: str = Query("kraken", max_length=30),
    market_type: str = Query("spot"),
    symbols: str = Query("", max_length=5100),
    timeframe: str = Query("1h"),
    confirm_timeframes: str = Query("", max_length=100),
    entry_timeframe: str = Query("", max_length=20),
    limit: int = Query(500, ge=250, le=1000),
    equity: float = Query(10000.0, gt=0, le=100_000_000),
    risk_percent: float = Query(1.0, gt=0, le=5),
    min_score: int = Query(70, ge=0, le=100),
    strategies: str = Query("trend_continuation_v2", max_length=200),
    auto_universe: bool | None = Query(None),
    quote_currencies: str = Query("USDT", max_length=100),
    max_markets: int = Query(30, ge=1, le=100),
    min_quote_volume: float = Query(1_000_000, ge=0),
    max_spread_bps: float | None = Query(None, gt=0, le=10_000),
    min_history_candles: int = Query(220, ge=0, le=5000),
    max_history_probes: int = Query(100, ge=1, le=500),
    max_ticker_requests: int = Query(200, ge=1, le=5000),
    fee_rate: float = Query(0.001, ge=0, le=0.05),
    slippage_bps: float = Query(5.0, ge=0, le=500),
):
    _validate_exchange(exchange, market_type, timeframe)
    confirmation_timeframes = _parse_timeframes(confirm_timeframes)
    entry_timeframe = entry_timeframe.strip()
    if entry_timeframe and entry_timeframe not in TIMEFRAMES:
        raise HTTPException(422, "Unsupported entry timeframe.")
    strategy_names = _parse_strategies(strategies)
    try:
        validate_confirmation_timeframes(timeframe, confirmation_timeframes)
        validate_entry_timeframe(timeframe, entry_timeframe or None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if auto_universe is None:
        auto_universe = not bool(symbols.strip())
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
                    max_ticker_requests=max_ticker_requests,
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
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
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






















@app.get("/api/backtest")
def backtest(
    exchange: str = Query("kucoin"),
    market_type: str = Query("spot"),
    symbol: str = Query("BTC/USDT", max_length=50),
    timeframe: str = Query("1h"),
    data_source: str = Query("local", pattern="^(local|live)$"),
    start: str | None = Query(None, max_length=32),
    end: str | None = Query(None, max_length=32),
    limit: int = Query(720, ge=250, le=1000),
    equity: float = Query(10000.0, ge=1, le=1e9),
    risk_percent: float = Query(.25, ge=.01, le=5),
    notional: float = Query(1000.0, ge=1, le=1e8),
    min_score: int = Query(70, ge=0, le=100),
    strategies: str = Query("trend_continuation_v2", max_length=200),
    confirm_timeframes: str = Query("4h", max_length=100),
    entry_timeframe: str = Query("15m", max_length=20),
    execution_timeframe: str = Query("", max_length=20),
    compare_exits: bool = Query(False),
    validate_oos: bool = Query(False),
    fee_rate: float = Query(0.001, ge=0, le=0.05),
    slippage_bps: float = Query(5.0, ge=0, le=500),
):
    _validate_exchange(exchange, market_type, timeframe)
    symbol = _validate_symbol(symbol)
    strategy_names = _parse_strategies(strategies)
    htf = _parse_timeframes(confirm_timeframes)
    ltf = entry_timeframe.strip() or None
    execution_tf = execution_timeframe.strip() or None
    try:
        policy = operator_policy(equity=equity, risk_percent=risk_percent,
            notional=notional, fee_rate=fee_rate, slippage_bps=slippage_bps)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    plan_config = TradePlanConfig(max_notional_value=notional, max_notional_percent=1.0,
        min_target_net_rr=1.0, min_blended_net_rr=1.0)
    from trading_bot.scanner.multi_timeframe import (
        validate_confirmation_timeframes,
        validate_entry_timeframe,
    )
    try:
        htf = validate_confirmation_timeframes(timeframe, htf)
        ltf = validate_entry_timeframe(timeframe, ltf)
        execution_tf = validate_entry_timeframe(timeframe, execution_tf)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if validate_oos and data_source != "local":
        raise HTTPException(422, "Chronological validation requires saved local multi-year data.")
    if (htf or ltf or execution_tf) and data_source != "local":
        raise HTTPException(
            422,
            "Historical multi-timeframe confirmation requires saved local "
            "datasets for every selected timeframe.",
        )

    try:
        secondary_data = {}
        warmup = INDICATOR_WINDOW
        requested_end_boundary = None
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

            # Saved data may include today's open bar. Never evaluate it.
            filtered = filtered[filtered["timestamp"] < latest_expected_close(pd.Timestamp.now(tz="UTC"), timeframe)]
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
                closure_timeframe = max((timeframe, *htf), key=timeframe_to_milliseconds)
                requested_end_boundary = latest_expected_close(
                    min(end_ts, pd.Timestamp.now(tz="UTC")), closure_timeframe)
                filtered = filtered[
                    filtered["timestamp"] < requested_end_boundary
                ]

            if start:
                start_ts = pd.Timestamp(start)
                start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
                prefix = filtered[filtered["timestamp"] < start_ts].tail(INDICATOR_WINDOW)
                selected = filtered[filtered["timestamp"] >= start_ts]
                if len(prefix) < INDICATOR_WINDOW:
                    raise HTTPException(422, f"The requested period needs {INDICATOR_WINDOW} earlier {timeframe} candles for indicator warmup. Prepare history first.")
                if selected.empty:
                    raise HTTPException(422, "No closed candles fall in the selected evaluation period.")
                filtered = pd.concat([prefix, selected], ignore_index=True)

            closed = filtered.reset_index(drop=True)
            source_path = str(dataset_path)

            for extra_timeframe in dict.fromkeys((*htf, *((ltf,) if ltf else ()), *((execution_tf,) if execution_tf else ()))):
                extra_store, extra_path, extra_checked = _history_store_for_dataset(
                    exchange, market_type, symbol, extra_timeframe
                )
                extra_candles = extra_store.load(
                    exchange, market_type, symbol, extra_timeframe
                )
                if extra_candles.empty:
                    checked = " | ".join(str(path) for path in extra_checked)
                    raise HTTPException(
                        404,
                        f"Missing {extra_timeframe} confirmation dataset. Checked: {checked}",
                    )
                secondary_data[extra_timeframe] = extra_candles
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
            min_rows=INDICATOR_WINDOW + 1,
        )
        if not quality.valid or quality.missing_intervals:
            raise HTTPException(
                422,
                "Historical dataset failed validation for "
                f"{exchange}/{market_type}/{symbol}/{timeframe}: "
                + "; ".join(quality.issues + quality.warnings)
                + (
                    f" Source: {source_path}"
                    if source_path is not None else ""
                ),
            )

        if validate_oos and len(closed) < 1500:
            raise HTTPException(422, "At least 1500 historical candles required for train/validation/test.")
        primary_delta = pd.to_timedelta(timeframe_to_milliseconds(timeframe), unit="ms")
        evaluation_start = pd.Timestamp(closed["timestamp"].iloc[warmup])
        evaluation_end = pd.Timestamp(closed["timestamp"].iloc[-1]) + primary_delta
        if requested_end_boundary is not None and evaluation_end != requested_end_boundary:
            raise HTTPException(422, "The primary dataset ends before the requested closed-candle period. Prepare complete matching history first.")
        for extra_timeframe, extra_candles in tuple(secondary_data.items()):
            delta = pd.to_timedelta(timeframe_to_milliseconds(extra_timeframe), unit="ms")
            first_close = latest_expected_close(evaluation_start + primary_delta, extra_timeframe)
            required_start = (first_close - INDICATOR_WINDOW * delta
                              if extra_timeframe in (*htf, ltf) else evaluation_start)
            required_end = latest_expected_close(evaluation_end, extra_timeframe)
            times = pd.to_datetime(extra_candles["timestamp"], utc=True)
            relevant = extra_candles[(times >= required_start) & (times < required_end)].reset_index(drop=True)
            check = validate_ohlcv(relevant, timeframe=extra_timeframe, min_rows=1)
            if (not check.valid or check.missing_intervals or relevant.empty
                    or pd.Timestamp(relevant["timestamp"].iloc[0]) != required_start
                    or pd.Timestamp(relevant["timestamp"].iloc[-1]) + delta != required_end):
                raise HTTPException(422, f"The {extra_timeframe} dataset does not cover the complete evaluation period and {INDICATOR_WINDOW}-candle warmup. Prepare matching history first.")
            secondary_data[extra_timeframe] = relevant

        result = run_decision_backtest(
            closed,
            initial_equity=equity,
            risk_percent=risk_percent / 100,
            min_confirmed_score=min_score,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            allow_short=market_type != "spot",
            strategy_names=strategy_names,
            primary_timeframe=timeframe,
            confirmation_timeframes=htf,
            entry_timeframe=ltf,
            secondary_data=secondary_data,
            execution_timeframe=execution_tf,
            warmup_candles=warmup,
            decision_lookback=INDICATOR_WINDOW,
            trade_plan_config=plan_config,
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

    exit_ablation = None
    if compare_exits:
        exit_ablation_frame, _ = run_exit_ablation(
            closed,
            initial_equity=equity,
            risk_percent=risk_percent / 100,
            min_confirmed_score=min_score,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            allow_short=market_type != "spot",
            strategy_names=strategy_names,
            primary_timeframe=timeframe,
            confirmation_timeframes=htf,
            entry_timeframe=ltf,
            execution_timeframe=execution_tf,
            secondary_data=secondary_data,
            warmup_candles=warmup,
            decision_lookback=INDICATOR_WINDOW,
            config=plan_config,
        )
        exit_ablation = _safe(exit_ablation_frame.to_dict("records"))

    oos_summary = None
    if validate_oos:
        if len(closed) < 1500:
            raise HTTPException(
                422, "At least 1500 historical candles required for train/validation/test."
            )
        train_cut = closed["timestamp"].iloc[int(len(closed) * 0.60)]
        validation_cut = closed["timestamp"].iloc[int(len(closed) * 0.80)]
        oos_frame, _ = run_out_of_sample_evaluation(
            closed,
            train_cutoff=train_cut,
            validation_cutoff=validation_cut,
            initial_equity=equity,
            risk_percent=risk_percent / 100,
            min_confirmed_score=min_score,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            allow_short=market_type != "spot",
            strategy_names=strategy_names,
            primary_timeframe=timeframe,
            confirmation_timeframes=htf,
            entry_timeframe=ltf,
            execution_timeframe=execution_tf,
            secondary_data=secondary_data,
            warmup_candles=warmup,
            decision_lookback=INDICATOR_WINDOW,
            trade_plan_config=plan_config,
        )
        oos_summary = _safe(oos_frame.to_dict("records"))

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
        "evaluation_start": _safe(evaluation_start),
        "evaluation_end": _safe(evaluation_end),
        "evaluation_rows": len(closed) - warmup,
        "warmup_candles": warmup,
        "policy": policy if (strategy_names == ("trend_continuation_v2",)
            and timeframe == "1h" and htf == ("4h",) and ltf == "15m"
            and min_score == 70 and execution_tf is None) else None,
        "research_variant": not (strategy_names == ("trend_continuation_v2",)
            and timeframe == "1h" and htf == ("4h",) and ltf == "15m"
            and min_score == 70 and execution_tf is None),
        "strategies": list(strategy_names),
        "confirmation_timeframes": list(htf),
        "entry_timeframe": ltf,
        "execution_timeframe": execution_tf,
        "assumptions": {
            "fee_rate_per_side": fee_rate,
            "slippage_bps_per_side": slippage_bps,
            "min_score": min_score,
            "indicator_window": INDICATOR_WINDOW,
            "risk_percent": risk_percent,
            "notional_cap": notional,
            "liquidity_replay": False,
            "portfolio_risk_replay": False,
            "single_timeframe_only": not bool(htf or ltf or execution_tf),
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
        "diagnostics": _safe(trade_diagnostics(result.trades, equity)),
        "execution_diagnostics": _safe(result.execution_diagnostics),
        "mark_to_market_equity": _safe(result.equity_curve),
        "exit_ablation": exit_ablation,
        "chronological_validation": oos_summary,
        "warning": (
            "This single-market historical test shares the signal policy; it does not replay live order-book checks or multi-market portfolio limits. Historical MTF confirmation is as-of closed candles. Intrabar "
            "fills are resolved with lower timeframe OHLC when an execution "
            "timeframe is selected; events inside each child candle remain "
            "ambiguous and are resolved conservatively."
            if htf or ltf or execution_tf else
            "Single-timeframe baseline; scanner MTF gates are not applied."
        ),
    }

