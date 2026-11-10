"""Regression coverage for chronological intrabar execution."""
from types import SimpleNamespace

import pandas as pd
import pytest

import trading_bot.backtesting.decision_engine as backtest_module
from trading_bot.backtesting import run_decision_backtest
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.planning import TradeSetup
from trading_bot.strategies import SetupAssessment


def decision():
    assessment = SetupAssessment(
        signal=Signal.LONG, status=SetupStatus.CONFIRMED,
        score=80, reason="synthetic", reasons=("synthetic",),
        strategy="trend_continuation",
    )
    setup = TradeSetup(
        signal=Signal.LONG, status=SetupStatus.CONFIRMED,
        score=80, reference_entry=100, entry_low=99, entry_high=101,
        stop_loss=95, take_profit_1=105, take_profit_2=110,
        reward_to_risk_1=1, reward_to_risk_2=2,
        position_size=2, notional_value=200,
        target_risk_amount=10, actual_risk_amount=10,
        reasons=("synthetic",),
        take_profit_1_fraction=.5,
        move_stop_to_break_even_after_tp1=True,
    )
    return SimpleNamespace(assessment=assessment, setup=setup,
        structure=SimpleNamespace(regime=MarketRegime.BULL_TREND))


def candles():
    dates = pd.date_range("2026-01-01", periods=4, freq="1h", tz="UTC")
    return pd.DataFrame([
        (dates[0], 100, 101, 99, 100, 1000),
        (dates[1], 100, 101, 99, 100, 1000),
        (dates[2], 100, 101, 99, 100, 1000),
        (dates[3], 100, 106, 99, 106, 1000),
    ], columns=("timestamp", "open", "high", "low", "close", "volume"))


def children():
    dates = pd.date_range("2026-01-01", periods=16, freq="15min", tz="UTC")
    data = []
    for i, t in enumerate(dates):
        if i == 12:
            row = (100, 101, 99, 100)
        elif i == 13:
            row = (100, 105.5, 100, 105)
        elif i >= 14:
            row = (105, 106, 104, 106)
        else:
            row = (100, 101, 99, 100)
        data.append((t, *row, 1000))
    return pd.DataFrame(data, columns=(
        "timestamp", "open", "high", "low", "close", "volume"
    ))


def test_intrabar_no_retroactive_breakeven(monkeypatch):
    class FakeEngine:
        def __init__(self, **kwargs): pass
        def evaluate(self, history): return decision()
    monkeypatch.setattr(backtest_module, "DecisionEngine", FakeEngine)
    baseline = run_decision_backtest(
        candles(), warmup_candles=1, fee_rate=0, slippage_bps=0
    )
    intrabar = run_decision_backtest(
        candles(), warmup_candles=1, fee_rate=0, slippage_bps=0,
        primary_timeframe="1h", execution_timeframe="15m",
        secondary_data={"15m": children()},
    )
    assert baseline.total_trades == intrabar.total_trades == 1
    assert baseline.trades.iloc[0].pnl == pytest.approx(5.0)
    assert intrabar.trades.iloc[0].pnl == pytest.approx(11.0)
    assert intrabar.trades.iloc[0].exit_reason == "END_OF_DATA_AFTER_TP1"


def test_incomplete_intrabar_history_fails_closed(monkeypatch):
    with pytest.raises(ValueError, match="Missing or misaligned"):
        run_decision_backtest(
            candles(), warmup_candles=1, execution_timeframe="15m",
            secondary_data={"15m": children().iloc[:-1]},
        )


def test_execution_timeframe_must_be_smaller():
    with pytest.raises(ValueError, match="lower than primary"):
        run_decision_backtest(
            candles(), warmup_candles=1, execution_timeframe="4h",
            secondary_data={"4h": children()},
        )
