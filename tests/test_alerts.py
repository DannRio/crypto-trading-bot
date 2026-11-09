from types import SimpleNamespace

import pandas as pd

from trading_bot.alerts import (
    AlertDispatcher,
    AlertEvent,
    AlertKind,
    AlertLedger,
    JsonlAlertSink,
    build_scan_alerts,
)
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.strategies import SetupAssessment


class CaptureSink:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)


def scan_result(status=SetupStatus.CONFIRMED):
    assessment = SetupAssessment(
        signal=Signal.LONG,
        status=status,
        score=82,
        reason="Trend aligned.",
        reasons=("Trend aligned.",),
    )
    return SimpleNamespace(
        exchange_id="kucoin",
        market_type="spot",
        symbol="BTC/USDT",
        timeframe="15m",
        assessment=assessment,
        setup=None,
        error=None,
        multi_timeframe_aligned=True,
    )


def test_build_scan_alerts_defaults_to_confirmed_only():
    events = build_scan_alerts(
        [
            scan_result(SetupStatus.CONFIRMED),
            scan_result(SetupStatus.FORMING),
        ],
        created_at="2026-01-01T00:00:00Z",
    )

    assert len(events) == 1
    assert events[0].kind == AlertKind.SETUP_CONFIRMED
    assert events[0].symbol == "BTC/USDT"
    assert events[0].score == 82


def test_alert_ledger_suppresses_across_dispatcher_restarts(tmp_path):
    path = tmp_path / "alerts.db"
    sink = CaptureSink()

    first = AlertEvent(
        kind=AlertKind.SETUP_CONFIRMED,
        created_at=pd.Timestamp(
            "2026-01-01T00:00:00Z"
        ),
        symbol="BTC/USDT",
        timeframe="15m",
        signal="LONG",
        status="CONFIRMED",
        score=82,
        message="Confirmed.",
    )

    ledger = AlertLedger(path)
    dispatcher = AlertDispatcher(
        [sink],
        ledger=ledger,
        cooldown_seconds=3600,
    )
    assert dispatcher.dispatch([first]) == (1, 0)
    ledger.close()

    second = AlertEvent(
        kind=AlertKind.SETUP_CONFIRMED,
        created_at=pd.Timestamp(
            "2026-01-01T00:30:00Z"
        ),
        symbol="BTC/USDT",
        timeframe="15m",
        signal="LONG",
        status="CONFIRMED",
        score=85,
        message="Still confirmed.",
    )

    ledger = AlertLedger(path)
    dispatcher = AlertDispatcher(
        [sink],
        ledger=ledger,
        cooldown_seconds=3600,
    )
    assert dispatcher.dispatch([second]) == (0, 1)
    ledger.close()

    assert len(sink.events) == 1


def test_jsonl_sink_writes_structured_event(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlAlertSink(path)
    event = AlertEvent(
        kind=AlertKind.SCAN_ERROR,
        created_at=pd.Timestamp(
            "2026-01-01T00:00:00Z"
        ),
        symbol="ETH/USDT",
        timeframe="1h",
        status="ERROR",
        message="Market scan failed.",
    )

    sink.send(event)

    text = path.read_text(encoding="utf-8")
    assert '"kind":"SCAN_ERROR"' in text
    assert '"symbol":"ETH/USDT"' in text
