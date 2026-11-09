from dataclasses import dataclass
import time
from typing import Callable

import pandas as pd

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.data import get_ohlcv
from trading_bot.paper.broker import PaperBroker
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.models import PaperAccountSnapshot
from trading_bot.scanner import (
    DynamicMarketUniverse,
    MarketScanner,
    UniverseConfig,
)
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
    symbols: tuple[str, ...] = ()
    auto_universe: bool = False
    quote_currencies: tuple[str, ...] = ("USDT",)
    max_markets: int = 20
    min_quote_volume: float = 1_000_000
    max_spread_bps: float | None = None
    candle_limit: int = 500
    starting_equity: float = 10_000
    risk_percent: float = 0.01
    min_confirmed_score: int = 70
    strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    entry_expiry_bars: int = 3
    interval_seconds: float = 60.0

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
    snapshot: PaperAccountSnapshot


class PaperTradingService:
    """Continuous read-only market scanner feeding the simulated broker."""

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
        self.broker = broker or PaperBroker(
            starting_equity=config.starting_equity,
            fee_rate=config.fee_rate,
            slippage_bps=config.slippage_bps,
            journal=journal,
        )

        if restore_state:
            self.broker.restore_from_journal()

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

    def _fetch_closed(self, symbol: str) -> pd.DataFrame:
        data = self.data_fetcher(
            symbol=symbol,
            timeframe=self.config.timeframe,
            limit=max(10, self.config.entry_expiry_bars + 3),
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
            candle_limit=self.config.candle_limit,
            account_equity=self.broker.account_equity(),
            risk_percent=self.config.risk_percent,
            min_confirmed_score=(
                self.config.min_confirmed_score
            ),
            strategy_names=self.config.strategy_names,
            data_fetcher=self.data_fetcher,
        )

    def run_once(self) -> PaperServiceRunReport:
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

        results = self._scanner().scan(scan_symbols)
        submitted = 0
        rejected = 0
        duplicates = 0
        scan_errors = 0

        for result in results:
            if result.error:
                scan_errors += 1
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
