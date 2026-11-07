"""Deterministic checks for scanner-aligned, conservative backtest fills."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_bot.analysis import add_indicators
from trading_bot.backtesting import run_backtest
from trading_bot.backtesting import engine
from trading_bot.core.enums import SetupStatus, Signal


def candles(n=230):
    close = np.full(n, 100.0)
    raw = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": close.copy(),
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.full(n, 200.0),
    })
    return raw


def install_one_setup(monkeypatch, signal=Signal.LONG):
    lengths = []

    def evaluate(history, structure, min_confirmed_score=70):
        lengths.append(len(history))
        status = SetupStatus.CONFIRMED if len(history) == 220 else SetupStatus.NO_TRADE
        return SimpleNamespace(
            status=status,
            signal=signal if status == SetupStatus.CONFIRMED else Signal.NO_TRADE,
            score=80,
            reasons=("Test scanner qualification",),
        )

    def plan(**kwargs):
        assert kwargs["signal"] == signal
        assert kwargs["score"] == 80
        assert kwargs["entry_price"] == 100.0
        if signal == Signal.LONG:
            stop, target = 95.0, 110.0
        else:
            stop, target = 105.0, 90.0
        return SimpleNamespace(
            signal=signal, entry_low=99.0, entry_high=101.0,
            stop_loss=stop, take_profit_2=target,
        )

    monkeypatch.setattr(engine, "analyze_market_structure", lambda history: None)
    monkeypatch.setattr(engine, "evaluate_trend_continuation", evaluate)
    monkeypatch.setattr(engine, "create_trade_setup", plan)
    return lengths


def test_scanner_signal_executes_next_candle_open_not_same_candle(monkeypatch):
    lengths = install_one_setup(monkeypatch)
    df = candles()
    df.loc[220, ["open", "high", "low", "close"]] = [100, 101, 94, 99]
    result = run_backtest(add_indicators(df), fee_rate=0.001, slippage_bps=5)
    assert result.signals_seen == 1
    assert result.total_trades == 1
    trade = result.trades.iloc[0]
    assert trade["signal_time"] == df.iloc[219]["timestamp"]
    assert trade["entry_time"] == df.iloc[220]["timestamp"]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["entry_price"] == pytest.approx(100.05)
    assert result.final_equity == pytest.approx(9900, abs=1e-6)
    assert lengths[0] == 220
    assert max(lengths) <= len(df) - 1


def test_gap_through_stop_exits_at_worse_open(monkeypatch):
    install_one_setup(monkeypatch)
    df = candles()
    df.loc[221, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    result = run_backtest(add_indicators(df))
    assert result.total_trades == 1
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_price"] == pytest.approx(90 * (1 - 5 / 10000))
    assert trade["pnl"] < -100  # Gap exceeded risk budget; do not hide it.


def test_entry_outside_scanner_zone_is_skipped(monkeypatch):
    install_one_setup(monkeypatch)
    df = candles()
    df.loc[220, ["open", "high", "low", "close"]] = [104, 105, 103, 104]
    result = run_backtest(add_indicators(df))
    assert result.signals_seen == 1
    assert result.entries_skipped == 1
    assert result.total_trades == 0
    assert result.final_equity == 10000


def test_both_stop_and_target_touched_chooses_stop(monkeypatch):
    install_one_setup(monkeypatch)
    df = candles()
    df.loc[220, ["open", "high", "low", "close"]] = [100, 111, 94, 101]
    result = run_backtest(add_indicators(df))
    assert result.trades.iloc[0]["exit_reason"] == "STOP_LOSS"


def test_spot_ignores_short_setups(monkeypatch):
    install_one_setup(monkeypatch, signal=Signal.SHORT)
    df = candles()
    result = run_backtest(add_indicators(df), allow_short=False)
    assert result.total_trades == 0
    assert result.signals_seen == 0
    result_short = run_backtest(add_indicators(df), allow_short=True)
    assert result_short.signals_seen == 1
    assert result_short.total_trades == 1


def test_input_validation_rejects_bad_config():
    df = add_indicators(candles())
    with pytest.raises(ValueError, match="Missing backtest columns"):
        run_backtest(df.drop(columns=["volume"]))
    with pytest.raises(ValueError, match="At least 220"):
        run_backtest(df.iloc[:219])
    with pytest.raises(ValueError, match="Minimum score"):
        run_backtest(df, min_confirmed_score=101)
    with pytest.raises(ValueError, match="positive and valid"):
        run_backtest(df, initial_equity=0)


def test_signal_check_uses_only_closed_history(monkeypatch):
    lengths = install_one_setup(monkeypatch)
    df = candles()
    run_backtest(add_indicators(df))
    assert lengths[0] == 220
    # While a position is open, no new setup is evaluated.
    assert all(220 <= n < len(df) for n in lengths)


def test_real_engine_remains_in_sync_with_live_scanner_import():
    from trading_bot.scanner import scanner
    assert engine.analyze_market_structure is scanner.analyze_market_structure
    assert engine.evaluate_trend_continuation is scanner.evaluate_trend_continuation
    assert engine.create_trade_setup is scanner.create_trade_setup
