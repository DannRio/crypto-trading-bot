"""Local-history dashboard regression tests; no network/exchange involved."""
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.data import HistoricalDataStore
from trading_bot.web import app as web

client = TestClient(web.app)


def create_history(store: HistoricalDataStore, rows: int = 320):
    frame = pd.DataFrame({
        "timestamp": pd.date_range(
            "2023-01-01", periods=rows, freq="h", tz="UTC"
        ),
        "open": [100.0] * rows,
        "high": [101.0] * rows,
        "low": [99.0] * rows,
        "close": [100.0] * rows,
        "volume": [1000.0] * rows,
    })
    store.upsert("kucoin", "spot", "BTC/USDT", "1h", frame)
    return frame


def test_history_list_includes_existing_spot_file(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path / "data" / "history")
    create_history(store)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    response = client.get("/api/history/datasets")
    assert response.status_code == 200, response.text
    datasets = response.json()["datasets"]
    assert len(datasets) == 1
    dataset = datasets[0]
    assert dataset["exchange"] == "kucoin"
    assert dataset["market_type"] == "spot"
    assert dataset["symbol"] == "BTC/USDT"
    assert dataset["timeframe"] == "1h"
    assert Path(dataset["path"]).is_file()


def test_check_exact_dataset_returns_candle_dates(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path / "data" / "history")
    create_history(store)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    response = client.get(
        "/api/history/check",
        params={
            "exchange": "kucoin", "market_type": "spot",
            "symbol": "BTC/USDT", "timeframe": "1h"
        }
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["found"] is True
    assert result["rows"] == 320
    assert result["start"].startswith("2023-01-01")
    assert result["end"].startswith("2023-01-14")


def test_mismatch_returns_expected_history_path(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path / "data" / "history")
    create_history(store)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    response = client.get(
        "/api/history/check",
        params={
            "exchange": "kucoin", "market_type": "spot",
            "symbol": "SOL/USDT", "timeframe": "15m"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["found"] is False
    assert "SOL_USDT" in data["path"]
    assert data["path"].endswith("15m.csv")


def test_history_backtest_rejects_wrong_symbol_with_clear_error(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path / "data" / "history")
    create_history(store)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    response = client.get(
        "/api/backtest",
        params={
            "source": "history", "exchange": "kucoin",
            "market_type": "spot", "symbol": "SOL/USDT",
            "timeframe": "1h"
        }
    )
    assert response.status_code == 422
    assert "No local history" in response.json()["detail"]
    assert "SOL_USDT" in response.json()["detail"]


def test_saved_history_backtest_is_independent_of_scanner(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path / "data" / "history")
    create_history(store)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    monkeypatch.setattr(web, "get_ohlcv", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("Saved history must not reach exchange API")
    ))
    response = client.get(
        "/api/backtest",
        params={
            "source": "history", "exchange": "kucoin",
            "market_type": "spot", "symbol": "BTC/USDT",
            "timeframe": "1h", "risk_percent": 1, "min_score": 70
        }
    )
    assert response.status_code == 200, response.text
    assert response.json()["data_rows"] == 320
    assert response.json()["source"] == "history"


def test_page_displays_independent_backtest_selection():
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="btExchange"' in response.text
    assert 'id="btDatasets"' in response.text
    assert 'id="btSymbol"' in response.text
    assert "Find saved history" in response.text
