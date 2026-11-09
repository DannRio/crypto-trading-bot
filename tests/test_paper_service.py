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
    assert service.broker.pending_orders[0].bars_remaining == 2

    third = service.run_once()
    assert third.new_candles == 0
    assert third.duplicate_signals == 1
    assert service.broker.pending_orders[0].bars_remaining == 2

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



class CorrelatedFeed:
    def __call__(
        self,
        symbol,
        timeframe="1h",
        limit=500,
        **kwargs,
    ):
        rows = 140
        base = [
            100.0 + index * 0.25
            for index in range(rows)
        ]
        if symbol == "ETH/USDT":
            base = [
                value * 2.0
                for value in base
            ]
        return pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    "2026-01-01",
                    periods=rows,
                    freq="1h",
                    tz="UTC",
                ),
                "open": base,
                "high": [value + 1 for value in base],
                "low": [value - 1 for value in base],
                "close": base,
                "volume": [1000.0] * rows,
            }
        )


class HighRiskScanner:
    def __init__(self, **kwargs):
        pass

    def scan(self, symbols):
        results = []
        for symbol in symbols:
            assessment = SetupAssessment(
                signal=Signal.LONG,
                status=SetupStatus.CONFIRMED,
                score=80,
                reason="correlated test",
                reasons=("correlated test",),
                strategy="trend_continuation",
            )
            setup = long_setup()
            setup = TradeSetup(
                signal=setup.signal,
                status=setup.status,
                score=setup.score,
                reference_entry=setup.reference_entry,
                entry_low=setup.entry_low,
                entry_high=setup.entry_high,
                stop_loss=setup.stop_loss,
                take_profit_1=setup.take_profit_1,
                take_profit_2=setup.take_profit_2,
                reward_to_risk_1=setup.reward_to_risk_1,
                reward_to_risk_2=setup.reward_to_risk_2,
                position_size=setup.position_size,
                notional_value=setup.notional_value,
                target_risk_amount=120.0,
                actual_risk_amount=120.0,
                reasons=setup.reasons,
            )
            results.append(
                SimpleNamespace(
                    symbol=symbol,
                    timeframe="1h",
                    assessment=assessment,
                    setup=setup,
                    structure=SimpleNamespace(
                        regime=MarketRegime.BULL_TREND
                    ),
                    error=None,
                )
            )
        return results


def test_service_applies_correlation_limit_to_new_setups(tmp_path):
    journal = PaperJournal(tmp_path / "paper.db")
    service = PaperTradingService(
        config=PaperServiceConfig(
            exchange_id="fake",
            market_type="spot",
            timeframe="1h",
            symbols=("BTC/USDT", "ETH/USDT"),
            fee_rate=0.0,
            slippage_bps=0.0,
            portfolio_risk=PortfolioRiskConfig(
                max_open_risk_percent=0.05,
                max_symbol_risk_percent=0.02,
                max_correlated_risk_percent=0.02,
                correlation_threshold=0.75,
                require_correlation_data=True,
            ),
        ),
        journal=journal,
        data_fetcher=CorrelatedFeed(),
        scanner_factory=HighRiskScanner,
    )

    report = service.run_once()

    assert report.submitted_orders == 1
    assert report.rejected_signals == 1
    events = journal.signal_events(limit=10)
    assert (
        events["reason"]
        .fillna("")
        .str.contains("correlated-risk")
        .any()
    )
    journal.close()
