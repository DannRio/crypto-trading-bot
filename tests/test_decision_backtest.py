from types import SimpleNamespace

import pandas as pd
import pytest

import trading_bot.backtesting.decision_engine as backtest_module
from trading_bot.backtesting import run_decision_backtest
from trading_bot.core.enums import (
    MarketRegime,
    Signal,
    SetupStatus,
)
from trading_bot.planning import TradeSetup
from trading_bot.strategies import SetupAssessment


def trending_candles(rows=320):
    prices = [100 + i * 0.5 for i in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01",
                periods=rows,
                freq="1h",
                tz="UTC",
            ),
            "open": prices,
            "high": [price + 1.25 for price in prices],
            "low": [price - 1.25 for price in prices],
            "close": prices,
            "volume": [1200.0] * rows,
        }
    )


def test_decision_backtest_records_strategy_diagnostics():
    result = run_decision_backtest(
        trending_candles(),
        min_confirmed_score=50,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert result.total_trades > 0
    assert "strategy" in result.trades.columns
    assert "score" in result.trades.columns
    assert "r_multiple" in result.trades.columns
    assert "tp1_hit" in result.trades.columns
    assert set(result.trades["signal"]) == {"LONG"}
    assert set(result.trades["strategy"]) == {
        "trend_continuation"
    }


def small_candles(rows):
    return pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )


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
        take_profit_1_fraction=0.5,
        move_stop_to_break_even_after_tp1=True,
    )


def confirmed_decision():
    assessment = SetupAssessment(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=80,
        reason="test",
        reasons=("test",),
        strategy="trend_continuation",
    )
    return SimpleNamespace(
        assessment=assessment,
        setup=long_setup(),
        structure=SimpleNamespace(
            regime=MarketRegime.BULL_TREND
        ),
    )


def no_trade_decision():
    assessment = SetupAssessment(
        signal=Signal.NO_TRADE,
        status=SetupStatus.NO_TRADE,
        score=0,
        reason="invalidated",
        reasons=("invalidated",),
        strategy="trend_continuation",
    )
    return SimpleNamespace(
        assessment=assessment,
        setup=None,
        structure=SimpleNamespace(
            regime=MarketRegime.RANGE
        ),
    )


def test_pending_setup_is_revalidated_before_later_fill(
    monkeypatch,
):
    class FakeDecisionEngine:
        def __init__(self, **kwargs):
            pass

        def evaluate(self, history):
            latest = float(history.iloc[-1]["close"])
            if latest == 101.0:
                return confirmed_decision()
            return no_trade_decision()

    monkeypatch.setattr(
        backtest_module,
        "DecisionEngine",
        FakeDecisionEngine,
    )

    times = pd.date_range(
        "2026-01-01",
        periods=4,
        freq="1h",
        tz="UTC",
    )
    data = small_candles(
        [
            [times[0], 100, 101, 100, 100, 1000],
            [times[1], 101, 102, 101, 101, 1000],
            [times[2], 102, 103, 101.5, 102, 1000],
            # This would fill the stale 99-101 zone if it survived.
            [times[3], 99.5, 100.0, 99.0, 99.5, 1000],
        ]
    )

    result = run_decision_backtest(
        data,
        warmup_candles=1,
        decision_lookback=21,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert result.total_trades == 0


def test_backtest_fills_entry_zone_without_reference_touch(
    monkeypatch,
):
    class FakeDecisionEngine:
        def __init__(self, **kwargs):
            pass

        def evaluate(self, history):
            return confirmed_decision()

    monkeypatch.setattr(
        backtest_module,
        "DecisionEngine",
        FakeDecisionEngine,
    )

    times = pd.date_range(
        "2026-01-01",
        periods=4,
        freq="1h",
        tz="UTC",
    )
    data = small_candles(
        [
            [times[0], 100, 101, 100, 100, 1000],
            [times[1], 101, 102, 101, 101, 1000],
            # Reference 100 is not touched, but the 99-101 zone is.
            [times[2], 99.5, 99.8, 99.2, 99.5, 1000],
            [times[3], 102, 103, 101, 102, 1000],
        ]
    )

    result = run_decision_backtest(
        data,
        warmup_candles=1,
        decision_lookback=21,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert result.total_trades == 1
    assert result.trades.iloc[0]["entry_price"] == pytest.approx(
        99.5
    )


def test_backtest_executes_tp1_and_break_even_stop(
    monkeypatch,
):
    class FakeDecisionEngine:
        def __init__(self, **kwargs):
            pass

        def evaluate(self, history):
            return confirmed_decision()

    monkeypatch.setattr(
        backtest_module,
        "DecisionEngine",
        FakeDecisionEngine,
    )

    times = pd.date_range(
        "2026-01-01",
        periods=5,
        freq="1h",
        tz="UTC",
    )
    data = small_candles(
        [
            [times[0], 100, 101, 100, 100, 1000],
            [times[1], 101, 102, 101, 101, 1000],
            [times[2], 100, 101, 99, 100, 1000],
            [times[3], 101, 106, 101, 105, 1000],
            [times[4], 103, 104, 99, 100, 1000],
        ]
    )

    result = run_decision_backtest(
        data,
        warmup_candles=1,
        decision_lookback=21,
        fee_rate=0.0,
        slippage_bps=0.0,
        allow_short=False,
    )

    assert result.total_trades == 1
    trade = result.trades.iloc[0]
    assert bool(trade["tp1_hit"]) is True
    assert trade["exit_reason"] == "STOP_LOSS_AFTER_TP1"
    assert trade["pnl"] == pytest.approx(5.0)
