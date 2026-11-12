"""One locally controlled paper worker and one persistent account.

Starting the web server does not submit orders or silently start a worker.
The explicit start/stop lifecycle owns only simulated trades and public data.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Event, RLock, Thread, current_thread

from trading_bot.config import operator_policy
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.risk import PortfolioRiskConfig
from trading_bot.paper.service import PaperServiceConfig, PaperTradingService


log = logging.getLogger(__name__)
EXCHANGES = {"kucoin", "kraken", "binance", "bybit", "okx", "coinbase"}
PAIR = re.compile(r"^[A-Z0-9._-]+/(USD|USDT|USDC)$")


def _safe(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "item"):
        return _safe(value.item())
    return None


class RuntimeConflict(ValueError):
    """A requested lifecycle change conflicts with the current account owner."""


@dataclass(frozen=True)
class BotSettings:
    exchange: str = "kucoin"
    quote: str = "USDT"
    symbols: tuple[str, ...] = ()
    max_markets: int = 10
    equity: float = 10_000
    risk_percent: float = 0.25
    notional: float = 1_000
    fee_rate: float = 0.001
    slippage_bps: float = 5

    def __post_init__(self):
        if self.exchange not in EXCHANGES:
            raise ValueError("Unsupported exchange.")
        if self.quote not in {"USD", "USDT", "USDC"}:
            raise ValueError("Choose USD, USDT, or USDC as the account quote.")
        if type(self.max_markets) is not int or not 1 <= self.max_markets <= 30:
            raise ValueError("Choose between 1 and 30 markets.")
        if not isinstance(self.symbols, (list, tuple)) or len(self.symbols) > 30:
            raise ValueError("Provide at most 30 exact spot pairs.")
        cleaned = []
        for symbol in self.symbols:
            if not isinstance(symbol, str) or len(symbol) > 50:
                raise ValueError("Use exact spot pairs such as BTC/USDT.")
            symbol = symbol.strip().upper()
            if not PAIR.fullmatch(symbol) or symbol.split("/")[1] != self.quote:
                raise ValueError("Watchlist pairs must use the selected account quote.")
            if symbol in cleaned:
                raise ValueError("Watchlist pairs must be unique.")
            cleaned.append(symbol)
        object.__setattr__(self, "symbols", tuple(cleaned))
        self.policy()

    def policy(self):
        return operator_policy(equity=self.equity, risk_percent=self.risk_percent,
            notional=self.notional, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps)


def build_service_config(settings: BotSettings):
    policy = settings.policy()
    values = policy["settings"]
    return PaperServiceConfig(
        exchange_id=settings.exchange, market_type="spot", symbols=settings.symbols,
        auto_universe=not bool(settings.symbols), quote_currencies=(settings.quote,),
        max_markets=settings.max_markets, min_quote_volume=1_000_000, max_spread_bps=40,
        min_history_candles=values["indicator_window"], max_history_probes=60,
        max_ticker_requests=200, universe_refresh_seconds=900,
        timeframe=values["timeframe"], confirmation_timeframes=values["confirmation_timeframes"],
        entry_timeframe=values["entry_timeframe"], candle_limit=values["candle_limit"],
        indicator_window=values["indicator_window"], starting_equity=settings.equity,
        risk_percent=values["risk_percent"], min_confirmed_score=values["min_confirmed_score"],
        strategy_names=values["strategy_names"], fee_rate=settings.fee_rate,
        slippage_bps=settings.slippage_bps, notional_cap=values["notional_cap"],
        min_blended_net_rr=values["min_blended_net_rr"], liquidity_preflight=True,
        policy_fingerprint=policy["fingerprint"], interval_seconds=60,
        portfolio_risk=PortfolioRiskConfig(max_open_risk_percent=.03, max_positions=5,
            max_pending_orders=5, max_symbol_risk_percent=.01, max_gross_notional_percent=1,
            max_correlated_risk_percent=.02, require_correlation_data=True,
            max_daily_loss_percent=.02, max_weekly_loss_percent=.05, max_drawdown_percent=.10),
    )


def _writer_active(path):
    from trading_bot.paper.locking import writer_active
    return writer_active(path)


class PaperRuntime:
    def __init__(self, journal_path, settings_path=None, *, service_factory=None,
                 journal_factory=None, owner_probe=None, now=None):
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.settings_path = Path(settings_path) if settings_path else self.journal_path.with_suffix(".settings.json")
        self.service_factory = service_factory or PaperTradingService
        self.journal_factory = journal_factory or PaperJournal
        self.owner_probe = owner_probe or _writer_active
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._thread = None
        self._stop = Event()
        self._settings = BotSettings()
        self._state = "stopped"
        self._health = "not_started"
        self._message = "Ready to start a paper account. No worker is running in this dashboard."
        self._started_at = None
        self._last_cycle_at = None
        self._last_error = None
        self._account = None
        self._last_cycle = None
        self._load_settings()

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat()

    def _owner(self):
        try:
            return self.owner_probe(self.journal_path)
        except Exception:
            return None

    def _load_settings(self):
        if not self.settings_path.is_file():
            return
        try:
            stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict) or stored.get("schema_version") != 1:
                raise ValueError("Unsupported saved configuration.")
            self._settings = BotSettings(**stored["settings"])
            self._message = "Saved settings loaded. Start explicitly to resume paper management."
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            self._state, self._health = "error", "configuration_error"
            self._last_error = "Saved bot settings could not be read. Review settings before starting."
            self._message = self._last_error

    def _save_settings(self, settings):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings_path.parent,
                                    prefix=".bot-settings-", suffix=".json", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"schema_version": 1, "settings": asdict(settings),
                           "policy": settings.policy(), "saved_at": self._timestamp()},
                          stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def status(self):
        with self._lock:
            active = self._thread is not None and self._thread.is_alive()
            result = {"state": self._state, "health": self._health, "active": active,
                "owner": "this_dashboard" if active else None, "message": self._message,
                "started_at": self._started_at, "last_cycle_at": self._last_cycle_at,
                "last_error": self._last_error, "config": asdict(self._settings),
                "policy_id": self._settings.policy()["policy_id"],
                "policy": self._settings.policy(), "account": self._account,
                "last_cycle": self._last_cycle, "mode": "paper", "live_orders": False}
        if not active:
            result["account"] = None
            owned = self._owner()
            if owned is True:
                result.update(state="external", active=True, owner="another_process", health="unknown",
                    message="Another process owns this paper account. Stop it there before starting here.")
            elif owned is None:
                result.update(health="ownership_unknown", message="Account ownership could not be checked.")
        elif result["state"] != "stopping":
            stamp = result["last_cycle_at"] or result["started_at"]
            if stamp and (self.now() - datetime.fromisoformat(stamp)).total_seconds() > 900:
                result.update(health="stale", message="The worker has not completed a cycle for over 15 minutes. Inspect its last error and stop it before restarting.")
        return _safe(deepcopy(result))

    def start(self, settings: BotSettings):
        if not isinstance(settings, BotSettings):
            raise TypeError("Validated bot settings are required.")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeConflict("The paper worker is already active in this dashboard.")
            if self._owner() is not False:
                raise RuntimeConflict("The account is owned by another process or ownership cannot be verified.")
            self._stop = Event()
            self._state, self._health = "starting", "starting"
            self._message = "Opening the saved account and checking its configuration."
            self._started_at = self._timestamp()
            self._last_error = self._last_cycle_at = self._last_cycle = self._account = None
            self._thread = Thread(target=self._run, args=(settings, self._stop),
                                  name="core-paper-bot", daemon=True)
            self._thread.start()
        return self.status()

    def _run(self, settings, stop_event):
        service = journal = None
        try:
            journal = self.journal_factory(self.journal_path)
            service = self.service_factory(config=build_service_config(settings), journal=journal)
            self._save_settings(settings)
            with self._lock:
                self._settings = settings
                self._account = _safe(service.broker.snapshot())
                if not stop_event.is_set():
                    self._message = "Account restored. Waiting for the first completed market-data cycle."
            service.run_forever(stop_event=stop_event, on_cycle=self._on_cycle, on_error=self._on_error)
        except Exception as exc:
            log.error("Paper worker stopped during startup or recovery (%s).", type(exc).__name__)
            message = "Paper startup or recovery failed. Existing account data was preserved."
            if "configuration does not match" in str(exc).lower():
                message = "These settings differ from the saved account. Restore its original settings or select a new journal with --data-dir."
            elif "persistence" in type(exc).__name__.lower() or "integrity" in type(exc).__name__.lower():
                message = "Account persistence or recovery reconciliation failed. The worker stopped; inspect the existing journal before restarting."
            elif "lock" in type(exc).__name__.lower() or "already" in str(exc).lower():
                message = "Another process owns this paper account. Stop that process before retrying."
            with self._lock:
                self._state, self._health = "error", "error"
                self._last_error = self._message = message
        finally:
            try:
                if service is not None:
                    service.close()
            except Exception as exc:
                log.error("Paper shutdown failed (%s).", type(exc).__name__)
                with self._lock:
                    self._state, self._health = "error", "error"
                    self._last_error = self._message = "Paper shutdown could not complete normally; verify the journal before restarting."
            finally:
                if journal is not None:
                    try:
                        journal.close()
                    except Exception as exc:
                        log.error("Paper journal close failed (%s).", type(exc).__name__)
                        with self._lock:
                            self._state, self._health = "error", "error"
                            self._last_error = self._message = "The account journal did not close normally. Check its ownership before restarting."
            with self._lock:
                if self._state != "error":
                    self._state, self._health = "stopped", "stopped"
                    self._message = "Stopped. Pending orders and positions are saved; their management resumes after restart."

    def _on_cycle(self, report):
        values = _safe(report)
        if not isinstance(values, dict):
            raise ValueError("Invalid paper cycle report.")
        degraded = bool(values.get("scan_errors") or values.get("management_errors") or values.get("universe_error"))
        halted = bool(values.get("risk_halted"))
        with self._lock:
            self._last_cycle = values
            self._account = values.get("snapshot")
            self._last_cycle_at = self._timestamp()
            self._health = "risk_halted" if halted else "degraded" if degraded else "ok"
            self._last_error = "Market data or position management is incomplete; inspect the recorded cycle." if degraded else None
            if not self._stop.is_set():
                self._state = "running"
                self._message = ("Risk protection is blocking new entries; existing positions remain managed." if halted else
                    "Market data is degraded; entries require all checks to pass." if degraded else
                    "Paper trading is running under the displayed policy.")

    def _on_error(self, exc):
        log.error("Paper cycle failed (%s).", type(exc).__name__)
        with self._lock:
            self._health = "error"
            self._last_error = "A paper cycle failed. The ledger was preserved; the worker will retry with fresh data."
            self._message = self._last_error
            if not self._stop.is_set():
                self._state = "running"

    def stop(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop.set()
                self._state = "stopping"
                self._message = "Stopping after the current bounded operation. Account state will be preserved."
                return self.status()
        if self._owner() is not False:
            raise RuntimeConflict("This dashboard cannot stop an external or unverified account owner. Stop it in its terminal or service.")
        return self.status()

    def shutdown(self, timeout=15):
        with self._lock:
            self._stop.set()
            worker = self._thread
            if worker is not None and worker.is_alive():
                self._state = "stopping"
        if worker is not None and worker is not current_thread():
            worker.join(timeout=timeout)
        return worker is None or not worker.is_alive()
