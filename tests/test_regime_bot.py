from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading_bot.regime_bot import (
    MarketRegime,
    RegimeBotConfig,
    RegimeSignalEngine,
    SignalAction,
    SetupType,
    to_dict,
)
from trading_bot.regime_bot.validation import drop_incomplete_candle


def make_trend(
    frequency: str,
    *,
    bars: int = 320,
    start: float = 100.0,
    slope: float = 0.05,
) -> pd.DataFrame:
    index = np.arange(bars)
    close = start + slope * index + 0.5 * np.sin(index / 5.0)
    open_price = np.r_[close[0] - slope, close[:-1]]
    high = np.maximum(open_price, close) + 0.25
    low = np.minimum(open_price, close) - 0.25
    volume = np.full(bars, 1_000.0)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01",
                periods=bars,
                freq=frequency,
                tz="UTC",
            ),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def make_breakout_frame() -> pd.DataFrame:
    bars = 300
    close = np.empty(bars)
    for index in range(bars):
        if index < 225:
            close[index] = 100.0 + 0.05 * index + 0.3 * np.sin(index / 7.0)
        elif index < 260:
            close[index] = 111.25 + 0.12 * np.sin(index / 3.0)
        else:
            close[index] = 111.25 + 0.12 * np.sin(index / 3.0)
    close[260:] += 0.45
    for index in range(261, bars):
        close[index] = close[260] + 0.06 * (index - 260) + 0.1 * np.sin(index / 5.0)

    volume = np.full(bars, 1_000.0)
    volume[260] = 2_600.0
    open_price = np.r_[close[0] - 0.05, close[:-1]]
    high = np.maximum(open_price, close) + 0.10
    low = np.minimum(open_price, close) - 0.10
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2024-01-01",
                periods=bars,
                freq="15min",
                tz="UTC",
            ),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def test_classifies_aligned_bullish_and_bearish_regimes() -> None:
    engine = RegimeSignalEngine()
    bullish = engine.analyze(
        symbol="BTC/USDT",
        frames={
            "15m": make_trend("15min", slope=0.05),
            "1h": make_trend("1h", slope=0.15),
            "4h": make_trend("4h", slope=0.40),
        },
        execution_timeframe="15m",
    )
    assert bullish.regime in {MarketRegime.BULLISH, MarketRegime.STRONG_BULLISH}
    assert bullish.action in {SignalAction.WATCH_LONG, SignalAction.LONG}
    assert bullish.consensus == pytest.approx(1.0)

    bearish = engine.analyze(
        symbol="BTC/USDT",
        frames={
            "15m": make_trend("15min", start=200.0, slope=-0.05),
            "1h": make_trend("1h", start=300.0, slope=-0.15),
            "4h": make_trend("4h", start=500.0, slope=-0.40),
        },
        execution_timeframe="15m",
    )
    assert bearish.regime in {MarketRegime.BEARISH, MarketRegime.STRONG_BEARISH}
    assert bearish.action in {SignalAction.WATCH_SHORT, SignalAction.SHORT}
    assert bearish.score < 0


def test_volume_backed_breakout_generates_complete_long_plan() -> None:
    frame = make_breakout_frame().iloc[:261]
    signal = RegimeSignalEngine().analyze(
        symbol="TEST/USDT",
        frames={"15m": frame},
        execution_timeframe="15m",
        equity=10_000.0,
        risk_percent=0.5,
    )
    assert signal.action == SignalAction.LONG
    assert signal.setup_type == SetupType.BREAKOUT
    assert signal.entry_plan is not None

    plan = signal.entry_plan
    assert plan.stop_loss < plan.reference_entry < plan.take_profit_1
    assert plan.take_profit_1 < plan.take_profit_2 < plan.take_profit_3
    assert plan.entry_low <= plan.entry_high
    assert plan.actual_risk_amount <= 50.0 + 1e-6
    assert plan.notional_value <= 10_000.0 + 1e-6
    assert plan.estimated_round_trip_cost > 0

    payload = to_dict(signal)
    assert payload["action"] == "long"
    assert payload["entry_plan"]["setup_type"] == "breakout"


def test_conflicting_higher_timeframe_blocks_execution_signal() -> None:
    execution = make_breakout_frame().iloc[:261]
    higher = make_trend("4h", start=300.0, slope=-0.35)
    signal = RegimeSignalEngine().analyze(
        symbol="TEST/USDT",
        frames={"15m": execution, "4h": higher},
        execution_timeframe="15m",
    )
    assert signal.action == SignalAction.NO_TRADE
    assert signal.entry_plan is None
    assert any("not sufficiently aligned" in reason for reason in signal.reasons)


def test_insufficient_data_fails_closed_instead_of_guessing() -> None:
    signal = RegimeSignalEngine().analyze(
        symbol="TEST/USDT",
        frames={"15m": make_trend("15min", bars=100)},
        execution_timeframe="15m",
    )
    assert signal.action == SignalAction.NO_TRADE
    assert signal.regime == MarketRegime.TRANSITION
    assert signal.entry_plan is None
    assert signal.warnings


def test_drop_incomplete_candle_removes_current_open_bar() -> None:
    frame = make_trend("15min", bars=10)
    latest_open = frame.iloc[-1]["timestamp"]
    before_close = latest_open + pd.Timedelta(minutes=10)
    result = drop_incomplete_candle(frame, "15m", now=before_close)
    assert len(result) == len(frame) - 1

    after_close = latest_open + pd.Timedelta(minutes=16)
    result = drop_incomplete_candle(frame, "15m", now=after_close)
    assert len(result) == len(frame)


def test_configuration_rejects_unknown_and_unsafe_values() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        RegimeBotConfig.from_dict({"signals": {"magic_prediction": True}})
    with pytest.raises(ValueError, match="Risk percentages"):
        RegimeBotConfig.from_dict(
            {"risk": {"risk_percent": 3.0, "maximum_risk_percent": 2.0}}
        )


def test_context_timeframe_must_be_higher_than_execution_timeframe() -> None:
    engine = RegimeSignalEngine()
    with pytest.raises(ValueError, match="Context timeframes must be higher"):
        engine.analyze(
            symbol="TEST/USDT",
            frames={
                "1h": make_trend("1h"),
                "15m": make_trend("15min"),
            },
            execution_timeframe="1h",
        )


def test_requested_context_with_insufficient_history_fails_closed() -> None:
    signal = RegimeSignalEngine().analyze(
        symbol="TEST/USDT",
        frames={
            "15m": make_trend("15min"),
            "4h": make_trend("4h", bars=100, slope=0.4),
        },
        execution_timeframe="15m",
    )
    assert signal.action == SignalAction.NO_TRADE
    assert signal.entry_plan is None
    assert any("every requested context" in warning for warning in signal.warnings)


def test_configuration_rejects_invalid_indicator_periods() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        RegimeBotConfig.from_dict({"indicators": {"rsi_period": 0}})
