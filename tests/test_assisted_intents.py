from types import SimpleNamespace

import pandas as pd
import pytest

from trading_bot.assisted import (
    IntentStatus,
    IntentStore,
    intent_from_scan_result,
    queue_scan_results,
)
from trading_bot.analysis import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.planning import TradeSetup
from trading_bot.scanner import ScanResult
from trading_bot.strategies import SetupAssessment


def scan_result(
    signal=Signal.LONG,
    market_type="spot",
    signal_time="2026-11-09T12:00:00Z",
):
    assessment = SetupAssessment(
        signal=signal,
        status=SetupStatus.CONFIRMED,
        score=82,
        reason="qualified setup",
        reasons=("qualified setup",),
        strategy="trend_continuation",
    )
    setup = TradeSetup(
        signal=signal,
        status=SetupStatus.CONFIRMED,
        score=82,
        reference_entry=100.0,
        entry_low=99.0,
        entry_high=101.0,
        stop_loss=95.0 if signal == Signal.LONG else 105.0,
        take_profit_1=105.0 if signal == Signal.LONG else 95.0,
        take_profit_2=110.0 if signal == Signal.LONG else 90.0,
        reward_to_risk_1=1.0,
        reward_to_risk_2=2.0,
        position_size=2.0,
        notional_value=200.0,
        target_risk_amount=10.0,
        actual_risk_amount=10.0,
        reasons=("qualified setup",),
    )
    structure = MarketStructure(
        regime=(
            MarketRegime.BULL_TREND
            if signal == Signal.LONG
            else MarketRegime.BEAR_TREND
        ),
        support=90.0,
        resistance=110.0,
        atr_pct=0.02,
        volume_ratio=1.2,
        higher_high=signal == Signal.LONG,
        higher_low=signal == Signal.LONG,
        lower_high=signal == Signal.SHORT,
        lower_low=signal == Signal.SHORT,
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
        confirmations=(),
        multi_timeframe_aligned=True,
        signal_time=pd.Timestamp(signal_time),
    )


def test_intent_expiry_is_based_on_signal_candle():
    result = scan_result()
    intent = intent_from_scan_result(
        result,
        expires_after_bars=3,
        created_at="2026-11-09T12:05:00Z",
    )

    assert intent.status == IntentStatus.PENDING_APPROVAL
    assert intent.signal_time == pd.Timestamp(
        "2026-11-09T12:00:00Z"
    )
    assert intent.expires_at == pd.Timestamp(
        "2026-11-09T15:00:00Z"
    )
    assert intent.plan["stop_loss"] == 95.0
    assert intent.plan["take_profit_2"] == 110.0


def test_queue_deduplicates_same_market_signal(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    result = scan_result()

    first = queue_scan_results(
        store,
        [result],
        now="2026-11-09T12:05:00Z",
    )
    second = queue_scan_results(
        store,
        [result],
        now="2026-11-09T12:10:00Z",
    )

    assert first.queued == 1
    assert second.duplicates == 1
    assert len(store.list()) == 1
    store.close()


def test_stale_signal_is_not_queued(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")

    report = queue_scan_results(
        store,
        [scan_result()],
        expires_after_bars=1,
        now="2026-11-09T13:01:00Z",
    )

    assert report.stale == 1
    assert report.queued == 0
    store.close()


def test_spot_short_is_not_queued_for_approval(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")

    report = queue_scan_results(
        store,
        [scan_result(signal=Signal.SHORT)],
        now="2026-11-09T12:05:00Z",
    )

    assert report.unsupported == 1
    assert report.queued == 0
    store.close()


def test_approval_and_rejection_are_audited(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    result = scan_result()
    queue_scan_results(
        store,
        [result],
        now="2026-11-09T12:05:00Z",
    )
    intent = store.list()[0]

    approved = store.approve(
        intent.intent_id,
        actor="Danny",
        note="Reviewed setup and risk.",
        decided_at="2026-11-09T12:10:00Z",
    )

    assert approved.status == IntentStatus.APPROVED
    decisions = store.decisions(intent.intent_id)
    assert len(decisions) == 1
    assert decisions[0].decision == "APPROVED"
    assert decisions[0].actor == "Danny"

    with pytest.raises(ValueError, match="only PENDING_APPROVAL"):
        store.reject(
            intent.intent_id,
            actor="Danny",
            decided_at="2026-11-09T12:11:00Z",
        )
    store.close()


def test_expired_intent_cannot_be_approved(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    queue_scan_results(
        store,
        [scan_result()],
        expires_after_bars=1,
        now="2026-11-09T12:05:00Z",
    )
    intent = store.list(
        statuses=(IntentStatus.PENDING_APPROVAL,),
        expire_at="2026-11-09T12:05:00Z",
    )[0]

    with pytest.raises(ValueError, match="EXPIRED"):
        store.approve(
            intent.intent_id,
            actor="Danny",
            decided_at="2026-11-09T13:01:00Z",
        )

    assert (
        store.get(intent.intent_id).status
        == IntentStatus.EXPIRED
    )
    store.close()


def test_plan_hash_detects_database_tampering(tmp_path):
    store = IntentStore(tmp_path / "assisted.db")
    queue_scan_results(
        store,
        [scan_result()],
        now="2026-11-09T12:05:00Z",
    )
    intent = store.list()[0]

    store.connection.execute(
        """
        UPDATE trade_intents
        SET plan_json = ?
        WHERE intent_id = ?
        """,
        ('{"stop_loss":0}', intent.intent_id),
    )
    store.connection.commit()

    with pytest.raises(
        ValueError,
        match="integrity check failed",
    ):
        store.get(intent.intent_id)
    store.close()
