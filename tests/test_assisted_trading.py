import pandas as pd
import pytest

from trading_bot.analysis import MarketStructure
from trading_bot.assisted import (
    AssistedTradingService,
    DryRunBrokerAdapter,
    IntentStatus,
    IntentStore,
)
from trading_bot.core.enums import (
    MarketRegime,
    Signal,
    SetupStatus,
)
from trading_bot.planning import TradeSetup
from trading_bot.scanner import ScanResult
from trading_bot.strategies import SetupAssessment


def confirmed_result(
    *,
    signal=Signal.LONG,
    market_type="spot",
    signal_time="2026-01-01T10:00:00Z",
):
    assessment = SetupAssessment(
        signal=signal,
        status=SetupStatus.CONFIRMED,
        score=82,
        reason="test",
        reasons=("test",),
        strategy="trend_continuation",
    )
    setup = TradeSetup(
        signal=signal,
        status=SetupStatus.CONFIRMED,
        score=82,
        reference_entry=100.0,
        entry_low=99.5,
        entry_high=100.5,
        stop_loss=95.0 if signal == Signal.LONG else 105.0,
        take_profit_1=105.0 if signal == Signal.LONG else 95.0,
        take_profit_2=110.0 if signal == Signal.LONG else 90.0,
        reward_to_risk_1=1.0,
        reward_to_risk_2=2.0,
        position_size=2.0,
        notional_value=200.0,
        target_risk_amount=10.0,
        actual_risk_amount=10.0,
        reasons=("test",),
    )
    structure = MarketStructure(
        regime=(
            MarketRegime.BULL_TREND
            if signal == Signal.LONG
            else MarketRegime.BEAR_TREND
        ),
        support=90.0,
        resistance=110.0,
        atr_pct=0.01,
        volume_ratio=1.2,
        higher_high=True,
        higher_low=True,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )
    return ScanResult(
        exchange_id="kucoin",
        market_type=market_type,
        symbol="BTC/USDT",
        timeframe="1h",
        assessment=assessment,
        structure=structure,
        setup=setup,
        data_quality=None,
        signal_time=pd.Timestamp(signal_time),
    )


def test_confirmed_result_queues_once_and_plan_is_immutable(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    service = AssistedTradingService(store)

    now = pd.Timestamp("2026-01-01T10:10:00Z")
    first = service.queue_scan_results(
        [confirmed_result()],
        now=now,
    )
    second = service.queue_scan_results(
        [confirmed_result()],
        now=now,
    )

    assert first.queued == 1
    assert second.duplicates == 1

    pending = service.pending(now=now)
    assert len(pending) == 1
    assert pending[0].status == IntentStatus.PENDING_APPROVAL
    assert pending[0].plan["stop_loss"] == 95.0

    store.connection.execute(
        """
        UPDATE trade_intents
        SET plan_json = ?
        WHERE intent_id = ?
        """,
        ('{"stop_loss":1}', pending[0].intent_id),
    )
    store.connection.commit()

    with pytest.raises(ValueError, match="integrity"):
        store.get(pending[0].intent_id)

    store.close()


def test_stale_setup_is_not_queued(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    service = AssistedTradingService(
        store,
        expiry_bars=1,
    )

    report = service.queue_scan_results(
        [confirmed_result()],
        now=pd.Timestamp("2026-01-01T11:00:00Z"),
    )

    assert report.queued == 0
    assert report.stale == 1
    assert service.pending(
        now=pd.Timestamp("2026-01-01T11:00:00Z")
    ) == []
    store.close()


def test_spot_short_is_rejected_by_broker_capability(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    service = AssistedTradingService(store)

    report = service.queue_scan_results(
        [
            confirmed_result(
                signal=Signal.SHORT,
                market_type="spot",
            )
        ],
        now=pd.Timestamp("2026-01-01T10:10:00Z"),
    )

    assert report.queued == 0
    assert report.unsupported == 1
    store.close()


def test_approval_executes_only_through_dry_run_adapter(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    broker = DryRunBrokerAdapter()
    service = AssistedTradingService(
        store,
        broker=broker,
    )
    service.queue_scan_results(
        [confirmed_result()],
        now=pd.Timestamp("2026-01-01T10:10:00Z"),
    )
    intent = service.pending(
        now=pd.Timestamp("2026-01-01T10:10:00Z")
    )[0]

    executed, receipt = service.approve(
        intent.intent_id,
        actor="Danny",
        note="Reviewed setup.",
        decided_at=pd.Timestamp("2026-01-01T10:15:00Z"),
    )

    assert executed.status == IntentStatus.EXECUTED
    assert receipt is not None
    assert receipt.mode == "DRY_RUN"
    assert receipt.accepted is True
    assert broker.capabilities.live_execution is False
    assert receipt.external_order_id is None
    assert receipt.plan_hash == intent.plan_hash
    assert len(store.execution_receipts(intent.intent_id)) == 1

    decisions = store.decisions(intent.intent_id)
    assert decisions[0].decision == "APPROVED"
    assert decisions[0].actor == "Danny"
    store.close()


def test_rejection_is_terminal_for_approval_workflow(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    service = AssistedTradingService(store)
    service.queue_scan_results(
        [confirmed_result()],
        now=pd.Timestamp("2026-01-01T10:10:00Z"),
    )
    intent = service.pending(
        now=pd.Timestamp("2026-01-01T10:10:00Z")
    )[0]

    rejected = service.reject(
        intent.intent_id,
        actor="Danny",
        note="Skip this setup.",
        decided_at=pd.Timestamp("2026-01-01T10:15:00Z"),
    )

    assert rejected.status == IntentStatus.REJECTED
    with pytest.raises(ValueError, match="only PENDING_APPROVAL"):
        service.approve(
            intent.intent_id,
            actor="Danny",
            decided_at=pd.Timestamp(
                "2026-01-01T10:20:00Z"
            ),
        )

    store.close()
