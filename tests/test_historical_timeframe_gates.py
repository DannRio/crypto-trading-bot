"""Regression tests for closed-candle, as-of historical MTF gates."""
from dataclasses import replace

import pandas as pd

from trading_bot.backtesting.timeframe_gates import HistoricalTimeframeGates
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.decision.models import DecisionResult
from trading_bot.strategies.models import SetupAssessment


def candles(freq, periods, trend):
    step = 0.20 if trend == "up" else -0.20
    start = 100.0 if trend == "up" else 500.0
    close = [start + step * i for i in range(periods)]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=periods, freq=freq, tz="UTC"),
        "open": close,
        "high": [p + 0.5 for p in close],
        "low": [p - 0.5 for p in close],
        "close": close,
        "volume": [1000.0] * periods,
    })


def long_decision():
    assessment = SetupAssessment(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=80,
        reason="test long",
        reasons=("test long",),
        strategy="trend_continuation",
    )
    return DecisionResult(assessment=assessment, structure=None, setup=object())


def test_future_four_hour_candles_cannot_confirm_past_signal():
    htf = candles("4h", 500, "up")
    gates = HistoricalTimeframeGates(
        "1h", {"4h": htf}, confirmation_timeframes=("4h",)
    )
    # 2025-01-10 has fewer than 400 fully closed 4h candles.
    result = gates.apply(long_decision(), pd.Timestamp("2025-01-10T00:00:00Z"))
    assert result.assessment.status == SetupStatus.FORMING
    assert result.setup is None


def test_opposite_closed_htf_regime_rejects_trade():
    gates = HistoricalTimeframeGates(
        "1h", {"4h": candles("4h", 500, "down")},
        confirmation_timeframes=("4h",),
    )
    result = gates.apply(long_decision(), pd.Timestamp("2025-03-15T00:00:00Z"))
    assert result.assessment.signal == Signal.NO_TRADE
    assert result.assessment.status == SetupStatus.NO_TRADE
    assert result.setup is None


def test_aligned_closed_htf_regime_preserves_confirmation():
    gates = HistoricalTimeframeGates(
        "1h", {"4h": candles("4h", 500, "up")},
        confirmation_timeframes=("4h",),
    )
    result = gates.apply(long_decision(), pd.Timestamp("2025-03-15T00:00:00Z"))
    assert result.assessment.status == SetupStatus.CONFIRMED
    assert result.setup is not None


def test_opposing_lower_timeframe_vetoes_entry():
    # At least 400 LTF candles are closed, and their EMA trend opposes LONG.
    gates = HistoricalTimeframeGates(
        "1h", {"15m": candles("15min", 1100, "down")},
        entry_timeframe="15m",
    )
    result = gates.apply(long_decision(), pd.Timestamp("2025-01-12T00:00:00Z"))
    assert result.assessment.status == SetupStatus.FORMING
    assert result.setup is None


def test_missing_secondary_history_fails_closed_at_configuration():
    import pytest
    with pytest.raises(ValueError, match="Missing historical timeframe data"):
        HistoricalTimeframeGates(
            "1h", {}, confirmation_timeframes=("4h",)
        )
