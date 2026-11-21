from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.regime_bot.api import create_app


def test_health_endpoint() -> None:
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["engine"] == "regime_signal_bot"


def test_analyze_endpoint_fails_closed_on_short_history() -> None:
    timestamps = pd.date_range("2025-01-01", periods=20, freq="15min", tz="UTC")
    candles = [
        {
            "timestamp": timestamp.isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000.0,
        }
        for timestamp in timestamps
    ]
    response = TestClient(create_app()).post(
        "/v1/analyze",
        json={
            "symbol": "TEST/USDT",
            "execution_timeframe": "15m",
            "frames": {"15m": candles},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "no_trade"
    assert payload["warnings"]
