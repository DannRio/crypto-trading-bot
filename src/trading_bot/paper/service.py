from dataclasses import asdict, dataclass, field
import json
import math
import time
from typing import Callable

import pandas as pd

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.data import get_ohlcv
from trading_bot.paper.broker import PaperBroker
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.models import PaperAccountSnapshot
from trading_bot.paper.risk import PortfolioRiskConfig
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
    candle_limit: int = 500
    starting_equity: float = 10_000
    risk_percent: float = 0.01
    min_confirmed_score: int = 70
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    entry_expiry_bars: int = 3
    interval_seconds: float = 60.0
    correlation_lookback: int = 120
    min_correlation_samples: int = 60
    portfolio_risk: PortfolioRiskConfig = field(
        default_factory=PortfolioRiskConfig
    )

    def __post_init__(self):
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive.")
        if not 0 < self.risk_percent <= 1:
            raise ValueError(
                "risk_percent must be greater than 0 and <= 1."
            )
        if self.fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError(
                "fee_rate and slippage_bps cannot be negative."
            )
        if self.entry_expiry_bars <= 0:
            raise ValueError("entry_expiry_bars must be positive.")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
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
    ):
        self.config = config
        self.journal = journal
        self.data_fetcher = data_fetcher
        self.scanner_factory = scanner_factory
        self.universe_factory = universe_factory
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
        }
        persisted = self.journal.metadata()

        if not persisted:
            for key, value in expected.items():
                self.journal.set_metadata(
                    key,
                    self._canonical_metadata_value(value),
                )
            return

        mismatches = []
        for key, value in expected.items():
            expected_value = self._canonical_metadata_value(
                value
            )
            actual = persisted.get(key)
            if actual is None:
                # Backward-compatible migration from the v0.10 journal.
                self.journal.set_metadata(
                    key,
                    expected_value,
                )
                continue
            if actual != expected_value:
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

    def _resolve_symbols(self) -> list[str]:
        if not self.config.auto_universe:
            return list(dict.fromkeys(self.config.symbols))

        selection = self.universe_factory(
            exchange_id=self.config.exchange_id,
            market_type=self.config.market_type,
        ).select(
            UniverseConfig(
                market_type=self.config.market_type,
                quote_currencies=self.config.quote_currencies,
                max_markets=self.config.max_markets,
                min_quote_volume=self.config.min_quote_volume,
                max_spread_bps=self.config.max_spread_bps,
                min_history_candles=(
                    self.config.min_history_candles
                ),
                history_timeframe=self.config.timeframe,
                max_history_probes=(
                    self.config.max_history_probes
                ),
            )
        )
        if not selection.symbols:
            raise RuntimeError(
                "Dynamic universe returned no eligible markets."
            )
        return selection.symbols

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
        if data.empty or len(data) < 2:
            return pd.DataFrame()

        closed = data.iloc[:-1].copy()
        if "timestamp" not in closed.columns:
            raise ValueError(
                "Paper service market data requires timestamp."
            )
        closed["timestamp"] = pd.to_datetime(
            closed["timestamp"],
            utc=True,
        )
        return (
            closed.sort_values("timestamp")
            .drop_duplicates(
                subset=["timestamp"],
                keep="last",
            )
            .reset_index(drop=True)
        )

    def _process_unseen_candles(
        self,
        symbol: str,
    ) -> tuple[int, int, pd.Timestamp | None]:
        closed = self._fetch_closed(symbol)
        if closed.empty:
            return 0, 0, None

        latest = closed.iloc[-1]
        latest_time = pd.Timestamp(latest["timestamp"])
        self.broker.last_prices[symbol] = float(
            latest["close"]
        )

        checkpoint = self.journal.get_checkpoint(
            symbol,
            self.config.timeframe,
        )
        if checkpoint is None:
            self.journal.set_checkpoint(
                symbol,
                self.config.timeframe,
                latest_time,
            )
            return 0, 0, latest_time

        checkpoint = pd.Timestamp(checkpoint)
        if checkpoint.tzinfo is None:
            checkpoint = checkpoint.tz_localize("UTC")
        else:
            checkpoint = checkpoint.tz_convert("UTC")

        unseen = closed[
            closed["timestamp"] > checkpoint
        ]
        processed = 0
        closed_trades = 0

        for _, candle in unseen.iterrows():
            trades = self.broker.process_candle(
                symbol=symbol,
                timeframe=self.config.timeframe,
                candle=candle,
            )
            self.journal.set_checkpoint(
                symbol,
                self.config.timeframe,
                candle["timestamp"],
            )
            processed += 1
            closed_trades += len(trades)

        return processed, closed_trades, latest_time

    def _scanner(self) -> MarketScanner:
        return self.scanner_factory(
            exchange_id=self.config.exchange_id,
            market_type=self.config.market_type,
            timeframe=self.config.timeframe,
            confirmation_timeframes=(
                self.config.confirmation_timeframes
            ),
            entry_timeframe=self.config.entry_timeframe,
            candle_limit=self.config.candle_limit,
            account_equity=self.broker.account_equity(),
            risk_percent=self.config.risk_percent,
            min_confirmed_score=(
                self.config.min_confirmed_score
            ),
            strategy_names=self.config.strategy_names,
            data_fetcher=self._cached_data_fetcher,
            fee_rate=self.config.fee_rate, slippage_bps=self.config.slippage_bps,
        )

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
        self._cycle_data_cache = {}
        scan_symbols = self._resolve_symbols()
        tracked_symbols = self._tracked_symbols(
            scan_symbols
        )

        new_candles = 0
        closed_trades = 0
        signal_times: dict[str, pd.Timestamp] = {}

        management_errors = 0
        for symbol in tracked_symbols:
            try:
                processed, closed_count, latest_time = (
                    self._process_unseen_candles(symbol)
                )
            except Exception:
                management_errors += 1
                continue
            new_candles += processed
            closed_trades += closed_count
            if latest_time is not None:
                signal_times[symbol] = latest_time

        risk_timestamp = (
            max(signal_times.values())
            if signal_times
            else pd.Timestamp.now(tz="UTC")
        )
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

        results = self._scanner().scan(tracked_symbols)

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

            try:
                self.broker.submit_setup(
                    symbol=result.symbol,
                    timeframe=self.config.timeframe,
                    setup=result.setup,
                    created_at=signal_time,
                    regime=(
                        result.structure.regime.value
                        if result.structure
                        else ""
                    ),
                    strategy=strategy,
                    expires_after_bars=(
                        self.config.entry_expiry_bars
                    ),
                    correlations=correlations,
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
                )
                rejected += 1
            else:
                self.journal.record_signal(
                    result.symbol,
                    self.config.timeframe,
                    strategy,
                    signal,
                    signal_time,
                    action="SUBMITTED",
                )
                submitted += 1

        return PaperServiceRunReport(
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
        )

    def run_forever(
        self,
        sleep_fn: Callable[[float], None] = time.sleep,
        on_cycle: Callable[[PaperServiceRunReport], None]
        | None = None,
        on_error: Callable[[Exception], None] | None = None,
        max_cycles: int | None = None,
    ) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                report = self.run_once()
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(exc)
            else:
                if on_cycle is not None:
                    on_cycle(report)

            cycles += 1
            if (
                max_cycles is not None
                and cycles >= max_cycles
            ):
                break
            sleep_fn(self.config.interval_seconds)
