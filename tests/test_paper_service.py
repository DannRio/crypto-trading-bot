from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.paper import (
    PaperBroker,
    PaperJournal,
    PaperServiceConfig,
    PaperTradingService,
    PortfolioRiskConfig,
)
from trading_bot.planning import TradeSetup
from trading_bot.strategies import SetupAssessment


def long_setup() -> TradeSetup:
    return TradeSetup(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=80,
        reference_entry=100.0,
        entry_low=99.0,
        entry_high=101.0,
        stop_loss=95.0,
        take_profit_1=105.0,
        take_profit_2=110.0,
        reward_to_risk_1=1.0,
        reward_to_risk_2=2.0,
        position_size=2.0,
        notional_value=200.0,
        target_risk_amount=10.0,
        actual_risk_amount=10.0,
        reasons=("test",),
    )


class FakeScanner:
    def __init__(self, **kwargs):
        pass

    def scan(self, symbols):
        assessment = SetupAssessment(
            signal=Signal.LONG,
            status=SetupStatus.CONFIRMED,
            score=80,
            reason="test",
            reasons=("test",),
            strategy="trend_continuation",
        )
        structure = SimpleNamespace(
            regime=MarketRegime.BULL_TREND
        )
        return [
            SimpleNamespace(
                symbol=symbol,
                timeframe="1h",
                assessment=assessment,
                setup=long_setup(),
                structure=structure,
                error=None,
            )
            for symbol in symbols
        ]


class CandleFeed:
    def __init__(self):
        self.rows = [
            [100.0, 101.0, 99.0, 100.0],
            [101.0, 102.0, 100.0, 101.0],
            [102.0, 103.0, 101.0, 102.0],
        ]

    def append(self, open_, high, low, close):
        self.rows.append([open_, high, low, close])

    def now(self):
        # The final fixture row is a currently forming one-hour candle.
        return pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(
            hours=len(self.rows) - 1, minutes=30,
        )

    def __call__(self, **kwargs):
        timestamps = pd.date_range(
            "2026-01-01",
            periods=len(self.rows),
            freq="1h",
            tz="UTC",
        )
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [row[0] for row in self.rows],
                "high": [row[1] for row in self.rows],
                "low": [row[2] for row in self.rows],
                "close": [row[3] for row in self.rows],
                "volume": [1000.0] * len(self.rows),
            }
        )


def test_journal_persists_checkpoint_and_signal_ledger(tmp_path):
    journal = PaperJournal(tmp_path / "paper.db")
    timestamp = pd.Timestamp("2026-01-01T01:00:00Z")

    assert journal.get_checkpoint("BTC/USDT", "1h") is None

    journal.set_checkpoint("BTC/USDT", "1h", timestamp)
    assert journal.get_checkpoint(
        "BTC/USDT",
        "1h",
    ) == timestamp

    assert not journal.signal_seen(
        "BTC/USDT",
        "1h",
        "trend_continuation",
        "LONG",
        timestamp,
    )

    journal.record_signal(
        "BTC/USDT",
        "1h",
        "trend_continuation",
        "LONG",
        timestamp,
        action="SUBMITTED",
    )

    assert journal.signal_seen(
        "BTC/USDT",
        "1h",
        "trend_continuation",
        "LONG",
        timestamp,
    )
    assert len(journal.signal_events()) == 1
    journal.close()


def test_broker_restores_open_position_and_account_state(tmp_path):
    path = tmp_path / "paper.db"
    journal = PaperJournal(path)
    broker = PaperBroker(
        starting_equity=10_000,
        fee_rate=0.001,
        slippage_bps=0.0,
        journal=journal,
    )

    broker.submit_setup(
        "BTC/USDT",
        "1h",
        long_setup(),
        "2026-01-01T00:00:00Z",
    )
    broker.process_candle(
        "BTC/USDT",
        "1h",
        {
            "timestamp": pd.Timestamp(
                "2026-01-01T01:00:00Z"
            ),
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
    )
    original = broker.snapshot()
    assert original.open_positions == 1

    restored = PaperBroker(
        starting_equity=10_000,
        fee_rate=0.001,
        slippage_bps=0.0,
        journal=journal,
    )
    state = restored.restore_from_journal()
    snapshot = restored.snapshot()

    assert state["open_positions"] == 1
    assert snapshot.open_positions == 1
    assert snapshot.fees_paid == pytest.approx(
        original.fees_paid
    )
    assert snapshot.realized_net_pnl == pytest.approx(
        original.realized_net_pnl
    )
    assert ("BTC/USDT", "1h") in restored.positions
    journal.close()


def test_service_processes_each_closed_candle_once(tmp_path):
    feed = CandleFeed()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            starting_equity=10_000,
            risk_percent=0.01,
            min_confirmed_score=70,
            fee_rate=0.0,
            slippage_bps=0.0,
            interval_seconds=1,
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )

    first = service.run_once()
    assert first.submitted_orders == 1
    assert first.new_candles == 0
    assert len(service.broker.pending_orders) == 1
    order = service.broker.pending_orders[0]
    assert order.bars_remaining == 3

    feed.append(103.0, 104.0, 102.0, 103.0)
    second = service.run_once()
    assert second.new_candles == 1
    assert second.rejected_signals == 1
    assert len(service.broker.pending_orders) == 0
    assert service.broker.snapshot().open_positions == 1

    third = service.run_once()
    assert third.new_candles == 0
    assert third.duplicate_signals == 1
    assert service.broker.snapshot().open_positions == 1

    journal.close()


def test_service_recovers_pending_order_after_restart(tmp_path):
    feed = CandleFeed()
    path = tmp_path / "paper.db"

    journal = PaperJournal(path)
    first_service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            fee_rate=0.0,
            slippage_bps=0.0,
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )
    first_service.run_once()
    assert len(first_service.broker.pending_orders) == 1
    journal.close()

    reopened = PaperJournal(path)
    second_service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            fee_rate=0.0,
            slippage_bps=0.0,
        ),
        journal=reopened,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )

    assert len(second_service.broker.pending_orders) == 1
    report = second_service.run_once()
    assert report.submitted_orders == 0
    assert report.duplicate_signals == 1
    reopened.close()



class FakeShortScanner:
    def __init__(self, **kwargs):
        pass

    def scan(self, symbols):
        assessment = SetupAssessment(
            signal=Signal.SHORT,
            status=SetupStatus.CONFIRMED,
            score=80,
            reason="test short",
            reasons=("test short",),
            strategy="trend_continuation",
        )
        short_setup = TradeSetup(
            signal=Signal.SHORT,
            status=SetupStatus.CONFIRMED,
            score=80,
            reference_entry=100.0,
            entry_low=99.0,
            entry_high=101.0,
            stop_loss=105.0,
            take_profit_1=95.0,
            take_profit_2=90.0,
            reward_to_risk_1=1.0,
            reward_to_risk_2=2.0,
            position_size=2.0,
            notional_value=200.0,
            target_risk_amount=10.0,
            actual_risk_amount=10.0,
            reasons=("test short",),
        )
        structure = SimpleNamespace(
            regime=MarketRegime.BEAR_TREND
        )
        return [
            SimpleNamespace(
                symbol=symbol,
                timeframe="1h",
                assessment=assessment,
                setup=short_setup,
                structure=structure,
                error=None,
            )
            for symbol in symbols
        ]


def test_spot_short_signal_is_rejected_once(tmp_path):
    feed = CandleFeed()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            fee_rate=0.0,
            slippage_bps=0.0,
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeShortScanner,
    )

    first = service.run_once()
    second = service.run_once()

    assert first.rejected_signals == 1
    assert first.submitted_orders == 0
    assert second.rejected_signals == 0
    assert second.duplicate_signals == 1
    assert len(journal.signal_events()) == 1
    assert (
        journal.signal_events().iloc[0]["reason"]
        == "Spot paper service is long-only."
    )
    journal.close()


def test_run_forever_can_continue_after_cycle_error(tmp_path):
    feed = CandleFeed()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            interval_seconds=1,
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )

    calls = {"count": 0}
    original = service.run_once

    def flaky_run_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary exchange failure")
        return original()

    service.run_once = flaky_run_once
    errors = []
    reports = []

    service.run_forever(
        sleep_fn=lambda seconds: None,
        on_cycle=reports.append,
        on_error=errors.append,
        max_cycles=2,
    )

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert len(reports) == 1
    journal.close()



def test_service_rejects_restart_with_different_account_invariants(
    tmp_path,
):
    path = tmp_path / "continuous.db"
    first = PaperJournal(path)
    PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            starting_equity=10_000,
            fee_rate=0.001,
            slippage_bps=5.0,
        ),
        journal=first,
        data_fetcher=CandleFeed(),
        scanner_factory=FakeScanner,
    )
    first.close()

    second = PaperJournal(path)
    with pytest.raises(
        ValueError,
        match="does not match the persisted journal",
    ):
        PaperTradingService(
            config=PaperServiceConfig(
                exchange_id="fake",
                market_type="spot",
                timeframe="15m",
                symbols=("BTC/USDT",),
                starting_equity=20_000,
                fee_rate=0.001,
                slippage_bps=5.0,
            ),
            journal=second,
            data_fetcher=CandleFeed(),
            scanner_factory=FakeScanner,
        )
    second.close()



def test_daily_loss_halt_cancels_pending_orders_and_persists(
    tmp_path,
):
    feed = CandleFeed()
    path = tmp_path / "continuous.db"
    journal = PaperJournal(path)
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            fee_rate=0.0,
            slippage_bps=0.0,
            portfolio_risk=PortfolioRiskConfig(
                max_daily_loss_percent=0.02,
                max_weekly_loss_percent=0.50,
                max_drawdown_percent=0.50,
                require_correlation_data=False,
            ),
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )

    first = service.run_once()
    assert first.risk_halted is False
    assert len(service.broker.pending_orders) == 1

    service.broker.realized_net_pnl = -300.0
    second = service.run_once()

    assert second.risk_halted is True
    assert "Daily loss limit" in second.risk_reason
    assert second.pending_cancelled_by_risk == 1
    assert len(service.broker.pending_orders) == 0

    persisted = journal.load_risk_state()
    assert persisted is not None
    assert persisted.halted is True
    journal.close()

    reopened = PaperJournal(path)
    restarted = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            fee_rate=0.0,
            slippage_bps=0.0,
            portfolio_risk=PortfolioRiskConfig(
                max_daily_loss_percent=0.02,
                max_weekly_loss_percent=0.50,
                max_drawdown_percent=0.50,
                require_correlation_data=False,
            ),
        ),
        journal=reopened,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )
    assert restarted.broker.risk_halted is True
    reopened.close()


def test_management_error_blocks_new_risk_for_cycle(tmp_path):
    class PartialFailureFeed(CandleFeed):
        def __call__(self, symbol, **kwargs):
            if symbol == "BROKEN/USDT":
                raise RuntimeError("market data unavailable")
            return super().__call__(symbol=symbol, **kwargs)

    feed = PartialFailureFeed()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT", "BROKEN/USDT"),
            fee_rate=0.0,
            slippage_bps=0.0,
            portfolio_risk=PortfolioRiskConfig(
                require_correlation_data=False,
            ),
        ),
        journal=journal,
        data_fetcher=feed,
        utc_now=feed.now,
        scanner_factory=FakeScanner,
    )

    report = service.run_once()

    assert report.management_errors == 1
    assert report.submitted_orders == 0
    assert report.rejected_signals >= 1
    journal.close()



def test_service_rejects_silent_risk_policy_change(tmp_path):
    path = tmp_path / "continuous.db"
    first = PaperJournal(path)
    PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT",),
            portfolio_risk=PortfolioRiskConfig(
                max_daily_loss_percent=0.02,
                require_correlation_data=False,
            ),
        ),
        journal=first,
        data_fetcher=CandleFeed(),
        scanner_factory=FakeScanner,
    )
    first.close()

    second = PaperJournal(path)
    with pytest.raises(
        ValueError,
        match="risk_policy",
    ):
        PaperTradingService(
            config=PaperServiceConfig(
                exchange_id="fake",
                market_type="spot",
                timeframe="1h",
                symbols=("BTC/USDT",),
                portfolio_risk=PortfolioRiskConfig(
                    max_daily_loss_percent=0.03,
                    require_correlation_data=False,
                ),
            ),
            journal=second,
            data_fetcher=CandleFeed(),
            scanner_factory=FakeScanner,
        )
    second.close()



def test_paper_service_uses_scanner_and_universe_quality_settings(
    tmp_path,
):
    captured = {}

    class CapturingUniverse:
        def __init__(self, **kwargs):
            captured["universe_init"] = kwargs

        def select(self, config):
            captured["universe_config"] = config
            return SimpleNamespace(
                symbols=["BTC/USDT"]
            )

    class CapturingScanner:
        def __init__(self, **kwargs):
            captured["scanner_init"] = kwargs

        def scan(self, symbols):
            return []

    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            confirmation_timeframes=("4h",),
            entry_timeframe="15m",
            auto_universe=True,
            min_history_candles=300,
            max_history_probes=25,
            portfolio_risk=PortfolioRiskConfig(
                require_correlation_data=False,
            ),
        ),
        journal=journal,
        data_fetcher=CandleFeed(),
        scanner_factory=CapturingScanner,
        universe_factory=CapturingUniverse,
    )

    assert service._resolve_symbols() == ["BTC/USDT"]
    service._scanner()

    universe_config = captured["universe_config"]
    assert universe_config.min_history_candles == 300
    assert universe_config.history_timeframe == "1h"
    assert universe_config.max_history_probes == 25
    assert universe_config.max_ticker_requests == 200

    scanner_config = captured["scanner_init"]
    assert scanner_config["confirmation_timeframes"] == ("4h",)
    assert scanner_config["entry_timeframe"] == "15m"

    journal.close()


class ServiceClock:
    def __init__(self):
        self.elapsed = 1000.0
        self.timestamp = pd.Timestamp("2026-01-01T02:30:00Z")

    def advance(self, seconds):
        self.elapsed += seconds
        self.timestamp += pd.Timedelta(seconds=seconds)

    def monotonic(self):
        return self.elapsed

    def utc_now(self):
        return self.timestamp


class SequencedUniverse:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0
        self.factory_calls = 0

    def factory(self, **kwargs):
        self.factory_calls += 1
        return self

    def select(self, config):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            symbols=outcome,
            warnings=("Some markets had no ticker volume.",),
            total_markets=100,
            eligible_markets=25,
            rejected={"missing_volume": 3},
        )


def auto_service(journal, feed, universe, clock):
    return PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            auto_universe=True,
            universe_refresh_seconds=60,
            fee_rate=0.0,
            slippage_bps=0.0,
        ),
        journal=journal,
        data_fetcher=feed,
        scanner_factory=FakeScanner,
        universe_factory=universe.factory,
        monotonic_clock=clock.monotonic,
        utc_now=clock.utc_now,
    )


@pytest.mark.parametrize("outcome", [RuntimeError("ticker outage"), []])
@pytest.mark.parametrize("already_filled", [False, True])
def test_discovery_failure_still_manages_pending_and_open_exposure(
    tmp_path, outcome, already_filled,
):
    feed = CandleFeed()
    feed.rows[-1] = [100.0, 101.0, 94.0, 94.0]
    feed.append(94.0, 95.0, 93.0, 94.0)
    journal = PaperJournal(tmp_path / "paper.db")
    clock = ServiceClock()
    clock.timestamp = feed.now()
    service = auto_service(
        journal, feed, SequencedUniverse([outcome]), clock,
    )
    service.broker.submit_setup(
        "BTC/USDT", "1h", long_setup(), "2026-01-01T00:00:00Z",
    )
    checkpoint = pd.Timestamp("2026-01-01T00:00:00Z")
    if already_filled:
        service.broker.process_candle(
            "BTC/USDT", "1h", feed().iloc[1],
        )
        checkpoint = pd.Timestamp("2026-01-01T01:00:00Z")
        assert service.broker.snapshot().open_positions == 1
    journal.set_checkpoint("BTC/USDT", "1h", checkpoint)

    report = service.run_once()

    assert report.scanned_markets == 0
    assert report.managed_markets == 1
    assert report.new_candles == (1 if already_filled else 2)
    assert report.closed_trades == 1
    assert report.submitted_orders == 0
    assert report.management_errors == 0
    assert report.universe_error
    assert report.universe_last_attempt_at
    assert report.universe_last_success_at is None
    assert service.broker.snapshot().open_positions == 0
    assert not service.broker.pending_orders
    status = json.loads(journal.metadata()["universe_status"])
    assert status["status"] == "error"
    assert status["entry_eligible"] is False
    if outcome == []:
        assert "no eligible markets" in report.universe_error
        assert status["latest_attempt_rejected"] == {"missing_volume": 3}
        assert report.universe_warnings
    heartbeat = json.loads(journal.metadata()["heartbeat"])
    assert heartbeat["status"] == "degraded"
    assert heartbeat["last_cycle_finished_at"]
    journal.close()


def test_universe_cache_uses_monotonic_ttl_and_reuses_selector(tmp_path):
    clock = ServiceClock()
    universe = SequencedUniverse([["BTC/USDT"], ["ETH/USDT"]])
    journal = PaperJournal(tmp_path / "paper.db")
    service = auto_service(journal, CandleFeed(), universe, clock)

    assert service._resolve_symbols() == ["BTC/USDT"]
    first = json.loads(journal.metadata()["universe_status"])
    assert first["status"] == "fresh"
    assert first["rejected"] == {"missing_volume": 3}

    # A wall-clock jump must not expire a valid in-process monotonic cache.
    clock.timestamp += pd.Timedelta(days=1)
    clock.advance(30)
    assert service._resolve_symbols() == ["BTC/USDT"]
    cached = json.loads(journal.metadata()["universe_status"])
    assert universe.calls == 1
    assert cached["status"] == "cached"
    assert cached["cache_age_seconds"] == 30
    assert cached["last_success_at"] == first["last_success_at"]
    assert cached["last_attempt_at"] == first["last_attempt_at"]
    assert cached["checked_at"] != first["checked_at"]

    clock.advance(30)
    assert service._resolve_symbols() == ["ETH/USDT"]
    refreshed = json.loads(journal.metadata()["universe_status"])
    assert universe.calls == 2
    assert universe.factory_calls == 1
    assert refreshed["status"] == "fresh"
    assert refreshed["last_success_at"] != first["last_success_at"]
    journal.close()


def test_failed_refresh_never_opens_entries_from_stale_universe(tmp_path):
    clock = ServiceClock()
    universe = SequencedUniverse([
        ["BTC/USDT"], RuntimeError("ticker outage"), ["ETH/USDT"],
    ])
    journal = PaperJournal(tmp_path / "paper.db")
    service = auto_service(journal, CandleFeed(), universe, clock)
    assert service._resolve_symbols() == ["BTC/USDT"]
    first = json.loads(journal.metadata()["universe_status"])

    clock.advance(60)
    report = service.run_once()
    failed = json.loads(journal.metadata()["universe_status"])
    assert report.scanned_markets == 0
    assert report.submitted_orders == 0
    assert report.universe_cached is False
    assert "ticker outage" in report.universe_error
    assert report.universe_last_success_at == first["last_success_at"]
    assert report.universe_last_attempt_at != first["last_attempt_at"]
    assert failed["symbols"] == ["BTC/USDT"]  # Historical selection only.
    assert failed["entry_eligible"] is False
    assert failed["cache_age_seconds"] is None

    # A failed refresh invalidates the TTL; the next cycle can recover.
    clock.advance(1)
    assert service._resolve_symbols() == ["ETH/USDT"]
    assert service._universe_error is None
    assert universe.factory_calls == 1
    assert universe.calls == 3
    journal.close()


def test_restart_requires_new_discovery_despite_persisted_snapshot(tmp_path):
    path = tmp_path / "paper.db"
    clock = ServiceClock()
    first = PaperJournal(path)
    initial = auto_service(
        first, CandleFeed(), SequencedUniverse([["BTC/USDT"]]), clock,
    )
    initial._resolve_symbols()
    prior = json.loads(first.metadata()["universe_status"])
    first.close()

    reopened = PaperJournal(path)
    universe = SequencedUniverse([RuntimeError("discovery unavailable")])
    restarted = auto_service(reopened, CandleFeed(), universe, clock)
    pending = json.loads(reopened.metadata()["universe_status"])
    assert pending["status"] == "pending_refresh"
    assert pending["entry_eligible"] is False
    assert restarted._resolve_symbols() == []
    current = json.loads(reopened.metadata()["universe_status"])
    assert universe.calls == 1
    assert current["status"] == "error"
    assert current["last_success_at"] == prior["last_success_at"]
    reopened.close()


@pytest.mark.parametrize("change", [
    {"strategy_names": ("trend_continuation_v2",)},
    {"risk_percent": 0.02},
    {"min_confirmed_score": 80},
    {"confirmation_timeframes": ("4h",)},
    {"entry_timeframe": "15m"},
    {"candle_limit": 600},
    {"entry_expiry_bars": 5},
])
def test_restart_rejects_changed_signal_policy(tmp_path, change):
    config = PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",))
    journal = PaperJournal(tmp_path / "paper.db")
    PaperTradingService(config=config, journal=journal)
    with pytest.raises(ValueError, match="signal_policy"):
        PaperTradingService(config=replace(config, **change), journal=journal)
    journal.close()


def test_restart_rejects_changed_universe_policy(tmp_path):
    config = PaperServiceConfig(exchange_id="fake", auto_universe=True)
    journal = PaperJournal(tmp_path / "paper.db")
    PaperTradingService(config=config, journal=journal)
    with pytest.raises(ValueError, match="universe_policy"):
        PaperTradingService(
            config=replace(config, max_markets=30), journal=journal,
        )
    journal.close()


def test_legacy_config_migration_is_explicit_and_does_not_hide_mismatch(tmp_path):
    config = PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",))
    journal = PaperJournal(tmp_path / "paper.db")
    PaperTradingService(config=config, journal=journal)
    journal.connection.execute(
        "DELETE FROM paper_service_metadata WHERE key IN ('signal_policy', 'universe_policy')"
    )
    journal.connection.commit()

    with pytest.raises(ValueError, match="starting_equity"):
        PaperTradingService(
            config=replace(config, starting_equity=20_000), journal=journal,
        )
    assert "signal_policy" not in journal.metadata()
    PaperTradingService(config=config, journal=journal)
    warnings = json.loads(journal.metadata()["config_migration_warnings"])
    assert len(warnings) == 2
    assert all("Earlier settings cannot be verified" in item for item in warnings)
    journal.close()


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_universe_refresh_interval_must_be_finite_and_positive(interval):
    with pytest.raises(ValueError, match="universe_refresh_seconds"):
        PaperServiceConfig(
            symbols=("BTC/USDT",), universe_refresh_seconds=interval,
        )


def test_heartbeat_records_success_and_uncaught_cycle_failure(tmp_path, monkeypatch):
    clock = ServiceClock()
    journal = PaperJournal(tmp_path / "paper.db")
    service = auto_service(
        journal, CandleFeed(), SequencedUniverse([["BTC/USDT"]]), clock,
    )
    service.run_once()
    success = json.loads(journal.metadata()["heartbeat"])
    assert success["status"] == "ok"
    assert success["last_cycle_started_at"]
    assert success["last_cycle_finished_at"]
    assert success["last_cycle_error"] is None

    def fail_before_management():
        raise RuntimeError("unexpected cycle failure")

    clock.advance(60)
    monkeypatch.setattr(service, "_resolve_symbols", fail_before_management)
    errors = []
    service.run_forever(max_cycles=1, on_error=errors.append)
    failed = json.loads(journal.metadata()["heartbeat"])
    assert len(errors) == 1
    assert failed["status"] == "error"
    assert "unexpected cycle failure" in failed["last_cycle_error"]
    assert failed["last_cycle_started_at"] != success["last_cycle_started_at"]
    journal.close()


@pytest.mark.parametrize(("as_of", "expected_rows"), [
    ("2026-01-01T02:59:59Z", 2),
    ("2026-01-01T03:00:00Z", 3),
])
def test_paper_candle_close_boundary_keeps_closed_only_last_row(
    tmp_path, as_of, expected_rows,
):
    data = CandleFeed()()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",)),
        journal=journal,
        data_fetcher=lambda **kwargs: data.copy(),
        scanner_factory=FakeScanner,
        utc_now=lambda: pd.Timestamp(as_of),
    )

    closed = service._fetch_closed("BTC/USDT")

    assert len(closed) == expected_rows
    assert closed.iloc[-1]["timestamp"] == data.iloc[expected_rows - 1]["timestamp"]
    journal.close()


@pytest.mark.parametrize("case", [
    "unordered", "duplicate", "gap", "nonfinite", "invalid_ohlc", "stale", "invalid_timestamp",
])
def test_invalid_paper_data_does_not_advance_checkpoint_or_open_risk(tmp_path, case):
    data = CandleFeed()()
    if case == "unordered":
        data = data.iloc[[1, 0, 2]]
    elif case == "duplicate":
        data = pd.concat([data, data.iloc[[-1]]], ignore_index=True)
    elif case == "gap":
        data = data.drop(index=1)
    elif case == "nonfinite":
        data.loc[1, "close"] = float("nan")
    elif case == "invalid_ohlc":
        data.loc[1, "high"] = 90.0
    elif case == "stale":
        data = data.iloc[:-1]
    else:
        data.loc[1, "timestamp"] = pd.NaT

    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",)),
        journal=journal,
        data_fetcher=lambda **kwargs: data.copy(),
        scanner_factory=FakeScanner,
        utc_now=lambda: pd.Timestamp("2026-01-01T03:00:00Z"),
    )
    with pytest.raises(ValueError):
        service._fetch_closed("BTC/USDT")

    report = service.run_once()

    assert report.management_errors == 1
    assert report.submitted_orders == 0
    assert journal.get_checkpoint("BTC/USDT", "1h") is None
    journal.close()


def test_paper_management_and_scanner_share_frozen_cycle_clock(tmp_path):
    clock = ServiceClock()
    clock.timestamp = pd.Timestamp("2026-01-01T03:00:00Z")
    observed = {}
    data = CandleFeed()()

    def slow_fetch(**kwargs):
        clock.advance(3600)
        return data.copy()

    class ClockScanner(FakeScanner):
        def __init__(self, now, **kwargs):
            observed["scanner_now"] = now()

    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",)),
        journal=journal,
        data_fetcher=slow_fetch,
        scanner_factory=ClockScanner,
        utc_now=clock.utc_now,
    )

    report = service.run_once()

    assert report.management_errors == 0
    assert observed["scanner_now"] == pd.Timestamp("2026-01-01T03:00:00Z")
    assert journal.get_checkpoint("BTC/USDT", "1h") == pd.Timestamp("2026-01-01T02:00:00Z")
    assert clock.timestamp == pd.Timestamp("2026-01-01T04:00:00Z")
    journal.close()


def test_strict_legacy_scanner_factory_remains_compatible(tmp_path):
    class LegacyScanner(FakeScanner):
        def __init__(
            self, exchange_id, market_type, timeframe, confirmation_timeframes,
            entry_timeframe, candle_limit, account_equity, risk_percent,
            min_confirmed_score, strategy_names, data_fetcher, fee_rate, slippage_bps,
        ):
            pass

    feed = CandleFeed()
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(exchange_id="fake", symbols=("BTC/USDT",)),
        journal=journal,
        data_fetcher=feed,
        scanner_factory=LegacyScanner,
        utc_now=feed.now,
    )
    assert service.run_once().submitted_orders == 1
    journal.close()
