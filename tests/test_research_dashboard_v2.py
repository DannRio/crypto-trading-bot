"""Dashboard regression tests for redesigned research controls."""
import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

import trading_bot.web.app as web


client = TestClient(web.app)


def history(rows=360):
    values = 100 + np.linspace(0, 25, rows) + np.sin(np.arange(rows) / 8)
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC"),
        "open": values,
        "high": values + 1,
        "low": values - 1,
        "close": values,
        "volume": [1000.0] * rows,
    })


def test_api_exposes_exit_ablation_and_cost_diagnostics(monkeypatch):
    monkeypatch.setattr(web, "get_ohlcv", lambda **kwargs: history())
    response = client.get(
        "/api/backtest",
        params={"data_source":"live", "compare_exits":True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["exit_variant"] for row in body["exit_ablation"]} == {
        "full_2r", "partial_no_be", "partial_be"
    }
    assert "by_strategy" in body["diagnostics"]
    assert "fees_total" in body["diagnostics"]
    assert "slippage_total" in body["diagnostics"]


def test_api_oos_rejects_short_live_history():
    response = client.get(
        "/api/backtest",
        params={"data_source":"live", "validate_oos":True},
    )
    assert response.status_code == 422
    assert "saved local" in response.json()["detail"]


def test_api_mtf_requires_local_saved_history():
    response = client.get(
        "/api/backtest",
        params={"data_source":"live", "confirm_timeframes":"4h"},
    )
    assert response.status_code == 422


def test_api_local_oos_requires_sufficient_bars(monkeypatch, tmp_path):
    dataset = web.HistoricalDataStore(tmp_path)
    dataset.upsert("kraken", "spot", "BTC/USD", "1h", history())
    monkeypatch.setattr(web, "HISTORY_ROOT", tmp_path)
    response = client.get(
        "/api/backtest",
        params={"data_source":"local", "validate_oos":True},
    )
    assert response.status_code == 422
    assert "1500" in response.json()["detail"]
