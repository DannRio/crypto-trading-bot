"""Verify the shared decision backtest's execution risk accounting."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import trading_bot.backtesting.decision_engine as bt
from trading_bot.core.enums import Signal, SetupStatus


def candles():
    n = 225
    close = np.full(n, 100.0)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": close.copy(),
        "high": close + 1,
        "low": close - 1,
        "close": close.copy(),
        "volume": np.full(n, 1000.0),
    })
    df.loc[222, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    return df


def fake_decisions(monkeypatch):
    seen = []

    class MockDecisionEngine:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def evaluate(self, history):
            confirmed = len(seen) == 1
            return SimpleNamespace(
                assessment=SimpleNamespace(
                    status=SetupStatus.CONFIRMED if confirmed else SetupStatus.NO_TRADE,
                    signal=Signal.LONG if confirmed else Signal.NO_TRADE,
                    score=80,
                ),
                setup=SimpleNamespace(
                    signal=Signal.LONG, reference_entry=100.0,
                    stop_loss=95.0, take_profit_2=110.0,
                    position_size=100.0,
                ) if confirmed else None,
                structure=SimpleNamespace(regime=SimpleNamespace(value="BULL_TREND")),
            )

    monkeypatch.setattr(bt, "DecisionEngine", MockDecisionEngine)
    return seen


def test_reference_order_fills_after_signal_then_gap_exits_at_worse_open(monkeypatch):
    seen = fake_decisions(monkeypatch)
    result = bt.run_decision_backtest(
        candles(), initial_equity=10000,
        risk_percent=0.01, fee_rate=0.001, slippage_bps=5,
    )
    assert len(seen) > 0
    assert result.total_trades == 1
    trade = result.trades.iloc[0]
    assert trade["signal_time"] < trade["entry_time"] < trade["exit_time"]
    assert trade["entry_price"] == pytest.approx(100.05)
    assert trade["exit_price"] == pytest.approx(90 * (1 - 0.0005))
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["r_multiple"] < -1  # Stop gaps can exceed modeled 1R.


def test_position_risk_includes_fees_and_stop_slippage(monkeypatch):
    fake_decisions(monkeypatch)
    df = candles()
    result = bt.run_decision_backtest(
        df, initial_equity=10000,
        risk_percent=0.01, fee_rate=0.002, slippage_bps=10,
    )
    trade = result.trades.iloc[0]
    assert trade["risk_amount"] <= 100.01
    expected = (
        (trade["entry_price"] - 95 * (1 - 0.001))
        + 0.002 * (trade["entry_price"] + 95 * (1 - 0.001))
    ) * trade["position_size"]
    assert trade["risk_amount"] == pytest.approx(expected)


def test_invalid_backtest_parameters_are_rejected():
    df = candles()
    for kw in ({"risk_percent": 1.2}, {"fee_rate": -0.01},
               {"slippage_bps": float("nan")}, {"initial_equity": -1},
               {"min_confirmed_score": 120}):
        with pytest.raises(ValueError):
            bt.run_decision_backtest(df, **kw)
