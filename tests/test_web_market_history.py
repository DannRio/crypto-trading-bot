"""End-to-end local dashboard checks without external exchange calls."""
import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.data import HistoricalDataStore
from trading_bot.web import app as web

client = TestClient(web.app)


def test_futures_selection_is_not_spot():
    response = client.get(
        "/api/scan?exchange=kraken&market_type=future&symbols=SOL/USDT"
    )
    assert response.status_code == 422
    assert "krakenfutures" in response.json()["detail"]
    response = client.get(
        "/api/backtest?exchange=krakenfutures&market_type=spot"
    )
    assert response.status_code == 422


def test_market_lookup_is_filtered_by_actual_ccxt_type(monkeypatch):
    class FakeClient:
        def __init__(self, exchange_id, market_type):
            assert exchange_id == "krakenfutures"
            assert market_type == "swap"

        def available_markets(self, search, limit):
            return [{"symbol": "SOL/USD:USD", "type": "swap",
                     "settle": "USD", "contract_size": 1.0}]

    monkeypatch.setattr(web, "CCXTClient", FakeClient)
    response = client.get(
        "/api/markets",
        params={"exchange": "krakenfutures", "market_type": "swap", "search": "SOL"},
    )
    assert response.status_code == 200
    assert response.json()["markets"][0]["symbol"] == "SOL/USD:USD"


def test_backtest_uses_local_history_without_live_api(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path)
    n = 320
    price = 100 + np.arange(n) * 0.02
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
        "volume": np.full(n, 1000),
    })
    store.upsert("kraken", "spot", "SOL/USD", "1h", frame)
    monkeypatch.setattr(web, "HistoricalDataStore", lambda: store)
    monkeypatch.setattr(web, "get_ohlcv", lambda **kw: (_ for _ in ()).throw(
        AssertionError("Live exchange must not be called for saved data")
    ))
    response = client.get(
        "/api/backtest",
        params={"exchange": "kraken", "market_type": "spot",
                "symbol": "SOL/USD", "timeframe": "1h",
                "source": "history", "min_score": 80},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "history"
    assert body["data_rows"] == n
    assert body["data_start"].startswith("2025-01-01")
    assert body["assumptions"]["min_confirmed_score"] == 80
    assert body["assumptions"]["shorts_enabled"] is False
