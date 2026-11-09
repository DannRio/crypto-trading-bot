"""Checks for research controls and explicit historical sample size."""
import numpy as np
import pandas as pd

from trading_bot.data import HistoricalDataStore
from trading_bot.research import batch as batch_module
from trading_bot.research.models import DatasetSpec


def make_data(n):
    prices = 100 + np.arange(n) * 0.05
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": prices,
        "high": prices + 1,
        "low": prices - 1,
        "close": prices,
        "volume": np.ones(n) * 1000,
    })


def test_batch_cli_accepts_allow_short_and_rejects_short_sample(tmp_path):
    store = HistoricalDataStore(tmp_path)
    spec = DatasetSpec("kucoin", "spot", "SOL/USDT", "1h")
    store.upsert("kucoin", "spot", "SOL/USDT", "1h", make_data(340))
    summary, details = batch_module.run_batch_research(
        [spec], store=store, allow_short=False,
        min_history_days=3 * 365.25,
    )
    assert summary.iloc[0]["status"] == "INSUFFICIENT_HISTORY"
    assert summary.iloc[0]["span_days"] < 15
    assert not details


def test_batch_enforces_spot_long_only_even_if_short_requested(tmp_path, monkeypatch):
    store = HistoricalDataStore(tmp_path)
    spec = DatasetSpec("kucoin", "spot", "SOL/USDT", "1h")
    store.upsert("kucoin", "spot", "SOL/USDT", "1h", make_data(340))
    observed = {}
    result = type("FakeResult", (), {
        "trades": pd.DataFrame(),
        "total_trades": 0,
        "total_return_pct": 0,
        "max_drawdown_pct": 0,
        "win_rate_pct": 0,
        "profit_factor": 0,
        "average_pnl": 0,
    })()

    def capture_backtest(**kwargs):
        observed.update(kwargs)
        return result

    monkeypatch.setattr(batch_module, "run_decision_backtest", capture_backtest)
    summary, _ = batch_module.run_batch_research(
        [spec], store=store, allow_short=True,
    )
    assert observed["allow_short"] is False
    assert summary.iloc[0]["status"] == "OK"


def test_exact_three_year_daily_archive_passes_duration_gate(tmp_path, monkeypatch):
    store = HistoricalDataStore(tmp_path)
    spec = DatasetSpec("kucoin", "spot", "SOL/USDT", "1d")
    timestamps = pd.date_range(
        "2023-01-01", "2026-01-01",
        freq="D", inclusive="left", tz="UTC",
    )
    data = pd.DataFrame({
        "timestamp": timestamps,
        "open": np.full(len(timestamps), 100.0),
        "high": np.full(len(timestamps), 101.0),
        "low": np.full(len(timestamps), 99.0),
        "close": np.full(len(timestamps), 100.0),
        "volume": np.full(len(timestamps), 1000.0),
    })
    store.upsert("kucoin", "spot", "SOL/USDT", "1d", data)
    fake = type("FakeResult", (), {
        "trades": pd.DataFrame(),
        "total_trades": 0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "average_pnl": 0.0,
    })()
    monkeypatch.setattr(batch_module, "run_decision_backtest", lambda **kw: fake)
    summary, _ = batch_module.run_batch_research(
        [spec], store=store, min_history_days=3 * 365.25,
    )
    assert summary.iloc[0]["status"] == "OK"
    assert summary.iloc[0]["span_days"] >= 1095.75
