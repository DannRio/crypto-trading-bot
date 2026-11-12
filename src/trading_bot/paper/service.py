from dataclasses import asdict, dataclass, field
import inspect
import json
import math
import time
from typing import Callable

import pandas as pd

from trading_bot.analysis.preflight import public_entry_preflight
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.data import get_ohlcv, timeframe_to_milliseconds, validate_ohlcv
from trading_bot.data.validation import latest_expected_close
from trading_bot.paper.broker import PaperBroker
from trading_bot.paper.journal import PaperJournal, PaperPersistenceError
from trading_bot.paper.models import PaperAccountSnapshot
from trading_bot.paper.risk import PortfolioRiskConfig
from trading_bot.planning import TradePlanConfig
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)
from trading_bot.scanner.multi_timeframe import regime_alignment
from trading_bot.strategies import (
    DEFAULT_STRATEGIES,
    normalize_strategy_names,
)


@dataclass(frozen=True)
class PaperServiceConfig:
    exchange_id: str = "kraken"
    market_type: str = "spot"
    timeframe: str = "1h"
    confirmation_timeframes: tuple[str, ...] = ()
    entry_timeframe: str | None = None
    symbols: tuple[str, ...] = ()
    auto_universe: bool = False
    quote_currencies: tuple[str, ...] = ("USDT",)
    max_markets: int = 20
    min_quote_volume: float = 1_000_000
    max_spread_bps: float | None = None
    min_history_candles: int = 220
    max_history_probes: int = 60
    max_ticker_requests: int = 200
    candle_limit: int = 500
    indicator_window: int = 400
    starting_equity: float = 10_000
    risk_percent: float = 0.01
    min_confirmed_score: int = 70
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    notional_cap: float = 1_000.0
    min_blended_net_rr: float = 1.0
    liquidity_preflight: bool = True
    policy_fingerprint: str | None = None
    entry_expiry_bars: int = 3
    interval_seconds: float = 60.0
    universe_refresh_seconds: float = 900.0
    correlation_lookback: int = 120
    min_correlation_samples: int = 60
    portfolio_risk: PortfolioRiskConfig = field(
        default_factory=PortfolioRiskConfig
    )

    def __post_init__(self):
        if not math.isfinite(self.starting_equity) or self.starting_equity <= 0:
            raise ValueError("starting_equity must be finite and positive.")
        if not math.isfinite(self.risk_percent) or not 0 < self.risk_percent <= 1:
            raise ValueError(
                "risk_percent must be greater than 0 and <= 1."
            )
        if (not math.isfinite(self.fee_rate) or not 0 <= self.fee_rate < 1
                or not math.isfinite(self.slippage_bps) or not 0 <= self.slippage_bps < 10_000):
            raise ValueError(
                "fee_rate and slippage_bps must be finite and within valid ranges."
            )
        if not math.isfinite(self.notional_cap) or self.notional_cap <= 0:
            raise ValueError("notional_cap must be finite and positive.")
        if not math.isfinite(self.min_blended_net_rr) or self.min_blended_net_rr <= 0:
            raise ValueError("min_blended_net_rr must be finite and positive.")
        if type(self.indicator_window) is not int or self.indicator_window < 220:
            raise ValueError("indicator_window must be an integer of at least 220.")
        if self.entry_expiry_bars <= 0:
            raise ValueError("entry_expiry_bars must be positive.")
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive.")
        if (
            not math.isfinite(self.universe_refresh_seconds)
            or self.universe_refresh_seconds <= 0
        ):
            raise ValueError("universe_refresh_seconds must be finite and positive.")
        if self.candle_limit < 2:
            raise ValueError("candle_limit must be at least 2.")
        if self.min_history_candles < 0:
            raise ValueError(
                "min_history_candles cannot be negative."
            )
        if self.max_history_probes <= 0:
            raise ValueError(
                "max_history_probes must be positive."
            )
        if self.max_ticker_requests <= 0:
            raise ValueError("max_ticker_requests must be positive.")
        if self.correlation_lookback < 2:
            raise ValueError(
                "correlation_lookback must be at least 2."
            )
        if self.min_correlation_samples < 2:
            raise ValueError(
                "min_correlation_samples must be at least 2."
            )
        if (
            self.min_correlation_samples
            > self.correlation_lookback
        ):
            raise ValueError(
                "min_correlation_samples cannot exceed "
                "correlation_lookback."
            )
        if not self.auto_universe and not self.symbols:
            raise ValueError(
                "Manual paper service requires at least one symbol."
            )
        normalize_strategy_names(self.strategy_names)


@dataclass(frozen=True)
class PaperServiceRunReport:
    scanned_markets: int
    managed_markets: int
    new_candles: int
    submitted_orders: int
    rejected_signals: int
    duplicate_signals: int
    closed_trades: int
    scan_errors: int
    management_errors: int
    pending_cancelled_by_risk: int
    risk_halted: bool
    risk_reason: str
    daily_loss_pct: float
    weekly_loss_pct: float
    drawdown_pct: float
    snapshot: PaperAccountSnapshot
    universe_error: str | None = None
    universe_warnings: tuple[str, ...] = ()
    universe_cached: bool = False
    universe_last_attempt_at: str | None = None
    universe_last_success_at: str | None = None
    management_error_details: tuple[str, ...] = ()


class PaperTradingService:
    """Continuous public-market scanner feeding the simulated broker."""

    def __init__(
        self,
        config: PaperServiceConfig,
        journal: PaperJournal,
        broker: PaperBroker | None = None,
        data_fetcher: Callable = get_ohlcv,
        scanner_factory: Callable = MarketScanner,
        universe_factory: Callable = DynamicMarketUniverse,
        restore_state: bool = True,
        monotonic_clock: Callable | None = None,
        utc_now: Callable | None = None,
        entry_preflight: Callable | None = None,
    ):
        self.config = config
        self.journal = journal
        self.data_fetcher = data_fetcher
        self.scanner_factory = scanner_factory
        self.universe_factory = universe_factory
        self.entry_preflight = entry_preflight or public_entry_preflight
        self._stop_event = None
        self._closed = False
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._utc_now = utc_now or (lambda: pd.Timestamp.now(tz="UTC"))
        self._universe_selector = None
        self._universe_symbols: list[str] = []
        self._universe_selected_monotonic: float | None = None
        self._universe_error: str | None = None
        self._universe_warnings: tuple[str, ...] = ()
        self._universe_cached = False
        self._cycle_as_of: pd.Timestamp | None = None
        self._cycle_data_cache: dict[
            tuple[str, str, int],
            pd.DataFrame,
        ] = {}
        self.broker = broker or PaperBroker(
            starting_equity=config.starting_equity,
            fee_rate=config.fee_rate,
            slippage_bps=config.slippage_bps,
            risk_config=config.portfolio_risk,
            journal=journal,
        )

        self._ensure_service_metadata()
        self._restore_universe_status()
        if restore_state:
            self.broker.restore_from_journal()

    @staticmethod
    def _canonical_metadata_value(value) -> str:
        if isinstance(value, float):
            return format(value, ".17g")
        return str(value)

    def _risk_policy_json(self) -> str:
        return json.dumps(
            {
                "portfolio_risk": asdict(
                    self.config.portfolio_risk
                ),
                "correlation_lookback": (
                    self.config.correlation_lookback
                ),
                "min_correlation_samples": (
                    self.config.min_correlation_samples
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _ensure_service_metadata(self) -> None:
        expected = {
            "exchange_id": self.config.exchange_id,
            "market_type": self.config.market_type,
            "timeframe": self.config.timeframe,
            "starting_equity": self.config.starting_equity,
            "fee_rate": self.config.fee_rate,
            "slippage_bps": self.config.slippage_bps,
            "risk_policy": self._risk_policy_json(),
            "signal_policy": json.dumps(
                {
                    "strategy_names": normalize_strategy_names(self.config.strategy_names),
                    "risk_percent": self.config.risk_percent,
                    "min_confirmed_score": self.config.min_confirmed_score,
                    "confirmation_timeframes": self.config.confirmation_timeframes,
                    "entry_timeframe": self.config.entry_timeframe,
                    "candle_limit": self.config.candle_limit,
                    "entry_expiry_bars": self.config.entry_expiry_bars,
                    "indicator_window": self.config.indicator_window,
                    "notional_cap": self.config.notional_cap,
                    "min_blended_net_rr": self.config.min_blended_net_rr,
                    "liquidity_preflight": self.config.liquidity_preflight,
                    "policy_fingerprint": self.config.policy_fingerprint,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "universe_policy": json.dumps(
                {
                    "auto_universe": self.config.auto_universe,
                    "symbols": sorted(set(self.config.symbols)) if not self.config.auto_universe else [],
                    "quote_currencies": sorted(set(quote.upper() for quote in self.config.quote_currencies)),
                    "max_markets": self.config.max_markets,
                    "min_quote_volume": self.config.min_quote_volume,
                    "max_spread_bps": self.config.max_spread_bps,
                    "min_history_candles": self.config.min_history_candles,
                    "max_history_probes": self.config.max_history_probes,
                    "max_ticker_requests": self.config.max_ticker_requests,
                    "universe_refresh_seconds": self.config.universe_refresh_seconds,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        persisted = self.journal.metadata()

        if not persisted:
            with self.journal.transaction():
                for key, value in expected.items():
                    self.journal.set_metadata(key, self._canonical_metadata_value(value))
            return

        mismatches = []
        missing = {}
        for key, value in expected.items():
            expected_value = self._canonical_metadata_value(
                value
            )
            actual = persisted.get(key)
            if actual is None:
                missing[key] = expected_value
                continue
            matches = actual == expected_value
            if not matches and key in {"risk_policy", "signal_policy", "universe_policy"}:
                try:
                    # JSON preserves integer/float spelling. Equivalent CLI
                    # and dashboard values (1000 vs 1000.0) are one policy.
                    matches = json.loads(actual) == json.loads(expected_value)
                except (TypeError, ValueError):
                    pass
            if not matches:
                mismatches.append(
                    f"{key}: journal={actual!r}, "
                    f"requested={expected_value!r}"
                )

        if mismatches:
            raise ValueError(
                "Paper service configuration does not match "
                "the persisted journal: "
                + "; ".join(mismatches)
                + ". Use the original settings or a new journal."
            )

        # Older journals did not record these settings. Preserve recovery,
        # but explicitly mark that prior signal configuration is unverified.
        # Do not mutate metadata at all if another invariant mismatched.
        with self.journal.transaction():
            for key, value in missing.items():
                self.journal.set_metadata(key, value)
            migrated_policies = sorted(set(missing) & {"signal_policy", "universe_policy"})
            if migrated_policies:
                self.journal.set_metadata(
                    "config_migration_warnings",
                    json.dumps([
                        f"Legacy journal did not record {key}; current settings establish "
                        "the restart baseline. Earlier settings cannot be verified."
                        for key in migrated_policies
                    ]),
                )

    def _now_timestamp(self) -> pd.Timestamp:
        timestamp = pd.Timestamp(self._utc_now())
        if pd.isna(timestamp):
            raise ValueError("Paper service clock returned an invalid timestamp.")
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp

    def _utc_timestamp(self) -> str:
        return self._now_timestamp().isoformat()

    def _decision_time(self) -> pd.Timestamp:
        if self._cycle_as_of is not None:
            return self._cycle_as_of
        return self._now_timestamp()

    def _restore_universe_status(self) -> None:
        raw = self.journal.metadata().get("universe_status", "{}")
        try:
            prior = json.loads(raw)
        except (TypeError, ValueError):
            prior = {}
        self._universe_status = prior if isinstance(prior, dict) else {}
        # A monotonic TTL cannot be restored across process lifetimes. Retain
        # prior timestamps for diagnosis, never restore entry eligibility.
        self._persist_universe_status(
            status="pending_refresh" if self.config.auto_universe else "manual",
            entry_eligible=False,
            cached=False,
            cache_age_seconds=None,
            error=None,
        )

    def _persist_universe_status(self, **updates) -> None:
        self._universe_status.update(updates)
        self._universe_status.update(
            {
                "mode": "auto" if self.config.auto_universe else "manual",
                "exchange_id": self.config.exchange_id,
                "market_type": self.config.market_type,
                "refresh_seconds": self.config.universe_refresh_seconds,
                "checked_at": self._utc_timestamp(),
            }
        )
        self.journal.set_metadata(
            "universe_status",
            json.dumps(self._universe_status, sort_keys=True),
        )

    def _persist_heartbeat(self, **updates) -> None:
        raw = self.journal.metadata().get("heartbeat", "{}")
        try:
            heartbeat = json.loads(raw)
        except (TypeError, ValueError):
            heartbeat = {}
        if not isinstance(heartbeat, dict):
            heartbeat = {}
        heartbeat.update(updates)
        self.journal.set_metadata(
            "heartbeat", json.dumps(heartbeat, sort_keys=True)
        )

    def _resolve_symbols(self) -> list[str]:
        self._universe_cached = False
        if not self.config.auto_universe:
            symbols = list(dict.fromkeys(self.config.symbols))
            self._persist_universe_status(
                status="manual", entry_eligible=bool(symbols), symbols=symbols,
                selected_markets=len(symbols), cached=False, error=None,
                warnings=[],
            )
            return symbols

        now = self._monotonic_clock()
        if self._universe_selected_monotonic is not None:
            age = now - self._universe_selected_monotonic
            if 0 <= age < self.config.universe_refresh_seconds:
                self._universe_cached = True
                self._persist_universe_status(
                    status="cached", entry_eligible=True, cached=True,
                    cache_age_seconds=age,
                )
                return list(self._universe_symbols)

        attempted_at = self._utc_timestamp()
        self._universe_error = None
        self._universe_warnings = ()
        selection = None
        try:
            # Keep the selector and its CCXT client alive so its rate limiter
            # and market cache survive refreshes.
            if self._universe_selector is None:
                self._universe_selector = self.universe_factory(
                    exchange_id=self.config.exchange_id,
                    market_type=self.config.market_type,
                )
            universe_config = UniverseConfig(
                    market_type=self.config.market_type,
                    quote_currencies=self.config.quote_currencies,
                    max_markets=self.config.max_markets,
                    min_quote_volume=self.config.min_quote_volume,
                    max_spread_bps=self.config.max_spread_bps,
                    min_history_candles=self.config.min_history_candles,
                    history_timeframe=self.config.timeframe,
                    max_history_probes=self.config.max_history_probes,
                    max_ticker_requests=self.config.max_ticker_requests,
                )
            select = self._universe_selector.select
            try:
                parameters = inspect.signature(select).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "cancel" in parameters:
                selection = select(universe_config, cancel=self._stop_event)
            else:
                selection = select(universe_config)
            self._universe_warnings = tuple(getattr(selection, "warnings", ()))
            symbols = list(dict.fromkeys(selection.symbols))
            if not symbols:
                raise RuntimeError("Dynamic universe returned no eligible markets.")
        except Exception as exc:
            self._universe_error = f"{type(exc).__name__}: {exc}"
            self._universe_selected_monotonic = None
            details = {}
            if selection is not None:
                details = {
                    "latest_attempt_total_markets": getattr(selection, "total_markets", None),
                    "latest_attempt_eligible_markets": getattr(selection, "eligible_markets", None),
                    "latest_attempt_rejected": getattr(selection, "rejected", {}),
                }
            self._persist_universe_status(
                status="error", entry_eligible=False, cached=False,
                cache_age_seconds=None,
                last_attempt_at=attempted_at, error=self._universe_error,
                warnings=list(self._universe_warnings), **details,
            )
            # Existing exposures are still included by _tracked_symbols.
            # Never create fresh risk from the last successful, now stale list.
            return []

        self._universe_symbols = symbols
        self._universe_selected_monotonic = self._monotonic_clock()
        self._persist_universe_status(
            status="fresh", entry_eligible=True, cached=False,
            last_attempt_at=attempted_at, last_success_at=self._utc_timestamp(),
            symbols=symbols, selected_markets=len(symbols), cache_age_seconds=0,
            total_markets=getattr(selection, "total_markets", None),
            eligible_markets=getattr(selection, "eligible_markets", None),
            rejected=getattr(selection, "rejected", {}),
            latest_attempt_total_markets=getattr(selection, "total_markets", None),
            latest_attempt_eligible_markets=getattr(selection, "eligible_markets", None),
            latest_attempt_rejected=getattr(selection, "rejected", {}),
            error=None, warnings=list(self._universe_warnings),
        )
        return list(symbols)

    def _tracked_symbols(
        self,
        scan_symbols: list[str],
    ) -> list[str]:
        symbols = set(scan_symbols)
        symbols.update(
            symbol
            for symbol, _ in self.broker.positions
        )
        symbols.update(
            order.symbol
            for order in self.broker.pending_orders
        )
        return sorted(symbols)

    def _cached_data_fetcher(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        exchange_id: str = "kraken",
        market_type: str = "spot",
        **kwargs,
    ) -> pd.DataFrame:
        key = (symbol, timeframe, limit)
        if key not in self._cycle_data_cache:
            self._cycle_data_cache[key] = self.data_fetcher(
                symbol=symbol,
                timeframe=timeframe,
                limit=limit,
                exchange_id=exchange_id,
                market_type=market_type,
                **kwargs,
            )
        return self._cycle_data_cache[key].copy()

    def _fetch_closed(self, symbol: str) -> pd.DataFrame:
        data = self._cached_data_fetcher(
            symbol=symbol,
            timeframe=self.config.timeframe,
            limit=self.config.candle_limit,
            exchange_id=self.config.exchange_id,
            market_type=self.config.market_type,
        )
        if data.empty:
            raise ValueError("Paper service received no usable market data.")
        if "timestamp" not in data.columns:
            raise ValueError(
                "Paper service market data requires timestamp."
            )
        timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
        if timestamps.isna().any():
            raise ValueError("Invalid or missing candle timestamp(s).")
        as_of = self._decision_time()
        delta = pd.to_timedelta(
            timeframe_to_milliseconds(self.config.timeframe), unit="ms",
        )
        closed = data.loc[timestamps + delta <= as_of].copy()
        closed["timestamp"] = timestamps.loc[closed.index]
        quality = validate_ohlcv(
            closed, timeframe=self.config.timeframe, min_rows=1,
        )
        if not quality.valid or quality.missing_intervals:
            raise ValueError("; ".join(quality.issues + quality.warnings))
        if (
            closed["timestamp"].iloc[-1] + delta
            != latest_expected_close(as_of, self.config.timeframe)
        ):
            raise ValueError("Stale candles: latest expected closed interval is missing.")
        return closed.reset_index(drop=True)

    def _process_unseen_candles(
        self,
        symbol: str,
    ) -> tuple[int, int, pd.Timestamp | None]:
        closed = self._fetch_closed(symbol)
        if closed.empty:
            return 0, 0, None

        latest = closed.iloc[-1]
        latest_time = pd.Timestamp(latest["timestamp"])

        checkpoint = self.journal.get_checkpoint(
            symbol,
            self.config.timeframe,
        )
        if checkpoint is None:
            if self.broker.has_exposure(symbol, self.config.timeframe):
                raise ValueError(
                    f"{symbol}: saved exposure has no candle checkpoint. "
                    "Management stopped; preserve this account and reconcile its missing history."
                )
            with self.broker.atomic_transition():
                self.broker.last_prices[symbol] = float(latest["close"])
                self.journal.set_checkpoint(symbol, self.config.timeframe, latest_time)
            return 0, 0, latest_time

        checkpoint = pd.Timestamp(checkpoint)
        if checkpoint.tzinfo is None:
            checkpoint = checkpoint.tz_localize("UTC")
        else:
            checkpoint = checkpoint.tz_convert("UTC")
        if pd.isna(checkpoint) or checkpoint > latest_time:
            raise ValueError(f"{symbol}: saved candle checkpoint is invalid or ahead of available closed history.")

        unseen = closed[
            closed["timestamp"] > checkpoint
        ]
        if not unseen.empty:
            expected = checkpoint + pd.to_timedelta(
                timeframe_to_milliseconds(self.config.timeframe), unit="ms")
            first = pd.Timestamp(unseen.iloc[0]["timestamp"])
            if first != expected:
                raise ValueError(
                    f"{symbol}: missing recovery history after {checkpoint.isoformat()}; "
                    f"expected {expected.isoformat()}, received {first.isoformat()}. "
                    "Management stopped and the checkpoint was preserved. Restore complete history before resuming."
                )
        processed = 0
        closed_trades = 0

        for _, candle in unseen.iterrows():
            if self._stop_event is not None and self._stop_event.is_set():
                break
            # Order, partial/terminal fills, risk state, and cursor form one
            # durable unit. A retry cannot apply a committed candle twice.
            with self.broker.atomic_transition():
                trades = self.broker.process_candle(
                    symbol=symbol, timeframe=self.config.timeframe, candle=candle)
                risk = self.broker.evaluate_account_risk(
                    candle["timestamp"] + pd.to_timedelta(
                        timeframe_to_milliseconds(self.config.timeframe), unit="ms"))
                if risk.state.halted:
                    self.broker.cancel_all_pending(reason="RISK_HALT")
                self.journal.set_checkpoint(symbol, self.config.timeframe, candle["timestamp"])
            processed += 1
            closed_trades += len(trades)

        if unseen.empty:
            self.broker.last_prices[symbol] = float(latest["close"])

        return processed, closed_trades, latest_time

    def _scanner(self) -> MarketScanner:
        kwargs = {
            "exchange_id": self.config.exchange_id,
            "market_type": self.config.market_type,
            "timeframe": self.config.timeframe,
            "confirmation_timeframes": self.config.confirmation_timeframes,
            "entry_timeframe": self.config.entry_timeframe,
            "candle_limit": self.config.candle_limit,
            "account_equity": self.broker.account_equity(),
            "risk_percent": self.config.risk_percent,
            "min_confirmed_score": self.config.min_confirmed_score,
            "strategy_names": self.config.strategy_names,
            "data_fetcher": self._cached_data_fetcher,
            "fee_rate": self.config.fee_rate,
            "slippage_bps": self.config.slippage_bps,
        }
        # Keep injected adapters with an explicit legacy signature usable.
        try:
            parameters = inspect.signature(self.scanner_factory).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        optional = {
            "now": self._decision_time,
            "indicator_window": self.config.indicator_window,
            "strategy_plan_configs": {
                strategy: TradePlanConfig(
                    max_notional_value=self.config.notional_cap,
                    max_notional_percent=1,
                    min_target_net_rr=1,
                    min_blended_net_rr=self.config.min_blended_net_rr,
                ) for strategy in self.config.strategy_names
            },
        }
        kwargs.update({key: value for key, value in optional.items() if accepts_kwargs or key in parameters})
        return self.scanner_factory(**kwargs)

    def _return_series(
        self,
        symbol: str,
    ) -> pd.Series | None:
        closed = self._fetch_closed(symbol)
        if len(closed) < self.config.min_correlation_samples + 1:
            return None

        series = (
            closed.set_index("timestamp")["close"]
            .astype(float)
            .pct_change()
            .replace([math.inf, -math.inf], pd.NA)
            .dropna()
            .tail(self.config.correlation_lookback)
        )
        if len(series) < self.config.min_correlation_samples:
            return None
        return series

    def _correlations_for(
        self,
        candidate_symbol: str,
    ) -> dict[str, float]:
        exposure_symbols = {
            position.symbol
            for position in self.broker.positions.values()
        } | {
            order.symbol
            for order in self.broker.pending_orders
        }
        exposure_symbols.discard(candidate_symbol)
        if not exposure_symbols:
            return {}

        candidate = self._return_series(
            candidate_symbol
        )
        if candidate is None:
            return {}

        correlations: dict[str, float] = {}
        for symbol in sorted(exposure_symbols):
            existing = self._return_series(symbol)
            if existing is None:
                continue

            joined = pd.concat(
                [
                    candidate.rename("candidate"),
                    existing.rename("existing"),
                ],
                axis=1,
                join="inner",
            ).dropna().tail(
                self.config.correlation_lookback
            )
            if (
                len(joined)
                < self.config.min_correlation_samples
            ):
                continue

            correlation = joined["candidate"].corr(
                joined["existing"]
            )
            if (
                correlation is not None
                and math.isfinite(float(correlation))
            ):
                correlations[symbol] = float(correlation)

        return correlations

    def run_once(self) -> PaperServiceRunReport:
        if self._closed:
            raise RuntimeError("This paper service is closed.")
        self.journal._require_writer()
        self._cycle_as_of = self._now_timestamp()
        self._persist_heartbeat(
            status="running",
            last_cycle_started_at=self._utc_timestamp(),
            last_cycle_error=None,
        )
        self._cycle_data_cache = {}
        scan_symbols = self._resolve_symbols()
        tracked_symbols = self._tracked_symbols(
            scan_symbols
        )

        new_candles = 0
        closed_trades = 0
        signal_times: dict[str, pd.Timestamp] = {}

        management_errors = 0
        management_error_details = []
        for symbol in tracked_symbols:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                processed, closed_count, latest_time = (
                    self._process_unseen_candles(symbol)
                )
            except PaperPersistenceError:
                raise
            except Exception as exc:
                management_errors += 1
                management_error_details.append(f"{symbol}: {type(exc).__name__}: {exc}")
                continue
            new_candles += processed
            closed_trades += closed_count
            if latest_time is not None:
                signal_times[symbol] = latest_time

        risk_timestamp = self._decision_time()
        risk_update = self.broker.evaluate_account_risk(
            risk_timestamp
        )

        pending_before = len(self.broker.pending_orders)
        if risk_update.state.halted:
            self.broker.cancel_all_pending(
                reason="RISK_HALT",
            )
        pending_cancelled = (
            pending_before - len(self.broker.pending_orders)
        )

        results = []
        if self._stop_event is None or not self._stop_event.is_set():
            scan = self._scanner().scan
            try:
                parameters = inspect.signature(scan).parameters
            except (TypeError, ValueError):
                parameters = {}
            results = (scan(tracked_symbols, cancel=self._stop_event)
                       if "cancel" in parameters else scan(tracked_symbols))

        result_by_symbol = {
            result.symbol: result
            for result in results
        }
        for order in list(self.broker.pending_orders):
            result = result_by_symbol.get(order.symbol)
            if result is None or result.error:
                continue
            still_valid = (
                result.assessment is not None
                and result.setup is not None
                and result.assessment.status
                == SetupStatus.CONFIRMED
                and result.assessment.signal == order.signal
                and result.assessment.strategy == order.strategy
            )
            if order.event_id:
                direction = 1 if order.signal == Signal.LONG else -1
                cancel = order.cancel_price if order.cancel_price is not None else order.stop_loss
                mark = self.broker.last_prices.get(order.symbol)
                expected = "RANGE" if order.setup_type == "range_reversal" else ("BULL_TREND" if direction == 1 else "BEAR_TREND")
                confirmations = getattr(result, "confirmations", ())
                contexts_ok = all(c.error is None and c.regime is not None
                                  and regime_alignment(order.signal, c.regime, order.strategy) is True for c in confirmations)
                still_valid = (mark is not None and direction * (mark - cancel) > 0
                               and result.structure is not None and result.structure.regime.value == expected
                               and contexts_ok)
            if not still_valid:
                self.broker.cancel_order(
                    order.order_id,
                    reason="SETUP_INVALIDATED",
                )

        submitted = 0
        rejected = 0
        duplicates = 0
        scan_errors = 0

        for result in results:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            if result.error:
                scan_errors += 1
                continue
            if result.symbol not in scan_symbols:
                continue
            if (
                result.assessment is None
                or result.setup is None
                or result.assessment.status
                != SetupStatus.CONFIRMED
            ):
                continue

            signal_time = signal_times.get(
                result.symbol
            )
            if signal_time is None:
                continue

            strategy = result.assessment.strategy
            signal = result.assessment.signal.value

            if self.journal.signal_seen(
                result.symbol,
                self.config.timeframe,
                strategy,
                signal,
                signal_time,
            ):
                duplicates += 1
                continue

            if management_errors:
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="REJECTED",
                    reason=(
                        "Risk data incomplete because one or more "
                        "managed markets failed to update."
                    ),
                )
                rejected += 1
                continue

            if risk_update.state.halted:
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="REJECTED",
                    reason=(
                        "Portfolio risk halt active: "
                        + risk_update.state.halt_reason
                    ),
                )
                rejected += 1
                continue

            if (
                self.config.market_type == "spot"
                and result.assessment.signal == Signal.SHORT
            ):
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="REJECTED",
                    reason="Spot paper service is long-only.",
                )
                rejected += 1
                continue

            if self.broker.has_exposure(
                result.symbol,
                self.config.timeframe,
            ):
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="SKIPPED_EXPOSURE",
                    reason="Existing pending/open exposure.",
                )
                rejected += 1
                continue

            correlations = self._correlations_for(
                result.symbol
            )

            evidence = {"policy_fingerprint": self.config.policy_fingerprint,
                        "liquidity_preflight": self.config.liquidity_preflight,
                        "signal_time": signal_time.isoformat()}
            try:
                setup = result.setup
                if self.config.liquidity_preflight:
                    preflight = self.entry_preflight(
                        exchange_id=self.config.exchange_id, market_type=self.config.market_type,
                        symbol=result.symbol, setup=setup, account_equity=self.broker.account_equity(),
                        risk_percent=self.config.risk_percent, notional_cap=self.config.notional_cap,
                        fee_rate=self.config.fee_rate, slippage_bps=self.config.slippage_bps,
                        min_blended_net_rr=self.config.min_blended_net_rr, now=self._now_timestamp,
                    )
                    evidence.update({key: value for key, value in preflight.items() if key != "setup"})
                    if not preflight.get("allowed") or preflight.get("setup") is None:
                        raise ValueError("; ".join(preflight.get("reasons") or ["Public entry preflight failed."]))
                    setup = preflight["setup"]
                submitted_at = self._now_timestamp()
                if self.config.liquidity_preflight:
                    expires = pd.Timestamp(preflight.get("expires_at"))
                    if pd.isna(expires) or expires.tzinfo is None or expires <= submitted_at:
                        raise ValueError("Public entry evidence is expired or its expiry is unverifiable.")
                if self._stop_event is not None and self._stop_event.is_set():
                    break
                with self.broker.atomic_transition():
                    order = self.broker.submit_setup(
                        symbol=result.symbol, timeframe=self.config.timeframe, setup=setup,
                        created_at=submitted_at,
                        regime=result.structure.regime.value if result.structure else "",
                        strategy=strategy, expires_after_bars=self.config.entry_expiry_bars,
                        correlations=correlations,
                    )
                    evidence.update(order_id=order.order_id, submitted_at=submitted_at.isoformat())
                    self.journal.record_signal(
                        result.symbol, self.config.timeframe, strategy, signal, signal_time,
                        action="SUBMITTED", evidence=evidence,
                    )
            except ValueError as exc:
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="REJECTED",
                    reason=str(exc),
                    evidence=evidence,
                )
                rejected += 1
            else:
                submitted += 1

        report = PaperServiceRunReport(
            scanned_markets=len(scan_symbols),
            managed_markets=len(tracked_symbols),
            new_candles=new_candles,
            submitted_orders=submitted,
            rejected_signals=rejected,
            duplicate_signals=duplicates,
            closed_trades=closed_trades,
            scan_errors=scan_errors,
            management_errors=management_errors,
            pending_cancelled_by_risk=pending_cancelled,
            risk_halted=risk_update.state.halted,
            risk_reason=risk_update.state.halt_reason,
            daily_loss_pct=risk_update.daily_loss_pct,
            weekly_loss_pct=risk_update.weekly_loss_pct,
            drawdown_pct=risk_update.drawdown_pct,
            snapshot=self.broker.snapshot(),
            universe_error=self._universe_error,
            universe_warnings=self._universe_warnings,
            universe_cached=self._universe_cached,
            universe_last_attempt_at=self._universe_status.get("last_attempt_at"),
            universe_last_success_at=self._universe_status.get("last_success_at"),
            management_error_details=tuple(management_error_details),
        )
        self._persist_heartbeat(
            status="degraded" if self._universe_error or management_errors or scan_errors else "ok",
            last_cycle_result="degraded" if self._universe_error or management_errors or scan_errors else "ok",
            last_cycle_finished_at=self._utc_timestamp(),
            last_cycle_error=None,
            management_errors=management_error_details,
        )
        return report

    def run_forever(
        self,
        sleep_fn: Callable[[float], None] = time.sleep,
        on_cycle: Callable[[PaperServiceRunReport], None]
        | None = None,
        on_error: Callable[[Exception], None] | None = None,
        max_cycles: int | None = None,
        stop_event=None,
    ) -> None:
        self._stop_event = stop_event
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                report = self.run_once()
            except Exception as exc:
                if not self.journal.writer_failed:
                    self._persist_heartbeat(
                        status="error", last_cycle_result="error",
                        last_cycle_finished_at=self._utc_timestamp(),
                        last_cycle_error=f"{type(exc).__name__}: {exc}",
                    )
                if on_error is None:
                    raise
                on_error(exc)
                if isinstance(exc, PaperPersistenceError) or self.journal.writer_failed:
                    raise
            else:
                if on_cycle is not None:
                    on_cycle(report)

            cycles += 1
            if (
                max_cycles is not None
                and cycles >= max_cycles
            ):
                break
            if stop_event is not None:
                if stop_event.wait(self.config.interval_seconds):
                    break
            else:
                sleep_fn(self.config.interval_seconds)

    def close(self) -> None:
        """Stop owning this account without closing its simulated positions."""
        if self._closed:
            return
        self._closed = True
        try:
            if not self.journal.writer_failed:
                self._persist_heartbeat(status="stopped", stopped_at=self._utc_timestamp())
        except Exception:
            # Closing the SQLite connection and writer lease must still happen.
            pass
        finally:
            self.journal.close()
