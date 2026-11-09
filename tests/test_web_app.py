"""Smoke-test read-only dashboard endpoints without external API calls."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.scanner import MarketCandidate, UniverseSelection
from trading_bot.web import app as web


client = TestClient(web.app)


def test_health_confirms_orders_disabled():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["live_execution"] is False
    assert response.json()["paper_execution"] is False


def test_dashboard_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "READ ONLY" in response.text


def test_reject_untrusted_market_inputs():
    for url in (
        "/api/scan?exchange=notreal",
        "/api/scan?symbols=BTCUSDT",
        "/api/scan?symbols=BTC/USD,BTC/USD",
        "/api/scan?risk_percent=30",
        "/api/scan?timeframe=1h&confirm_timeframes=15m",
        "/api/backtest?symbol=%2F%2F",
        "/api/backtest?limit=50",
        "/api/scan?strategies=not_real",
    ):
        assert client.get(url).status_code == 422, url


def test_scanner_respects_market_parameters(monkeypatch):
    captured = {}

    class FakeScanner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def scan(self, symbols):
            captured["symbols"] = symbols
            assessment = SimpleNamespace(
                status=SimpleNamespace(value="CONFIRMED"),
                signal=SimpleNamespace(value="LONG"),
                score=83,
                reason="Bull trend and volume confirmation",
                strategy="trend_continuation",
            )
            return [
                SimpleNamespace(
                    symbol=symbol,
                    assessment=assessment,
                    score=83,
                    structure=None,
                    setup=None,
                    data_quality=None,
                    error=None,
                )
                for symbol in symbols
            ]

    monkeypatch.setattr(web, "MarketScanner", FakeScanner)
    response = client.get(
        "/api/scan",
        params={
            "exchange": "bybit",
            "market_type": "swap",
            "symbols": "BTC/USDT:USDT,ETH/USDT:USDT",
            "timeframe": "15m",
            "confirm_timeframes": "1h,4h",
            "equity": 25000,
            "risk_percent": 1.5,
            "strategies": "trend_continuation,breakout_retest",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["status"] == "CONFIRMED"
    assert body["live_execution"] is False
    assert captured["risk_percent"] == 0.015
    assert captured["market_type"] == "swap"
    assert captured["confirmation_timeframes"] == ("1h", "4h")
    assert captured["strategy_names"] == (
        "trend_continuation",
        "breakout_retest",
    )
    assert captured["symbols"] == ["BTC/USDT:USDT", "ETH/USDT:USDT"]


def test_backtest_runs_on_synthetic_candles(monkeypatch):
    n = 330
    base = 100 + np.linspace(0, 25, n) + 2 * np.sin(np.arange(n) / 10)
    candles = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": base - 0.2,
        "high": base + 1.0,
        "low": base - 1.0,
        "close": base,
        "volume": np.full(n, 500.0),
    })
    monkeypatch.setattr(web, "get_ohlcv", lambda **kwargs: candles.copy())
    response = client.get(
        "/api/backtest?symbol=BTC/USD"
        "&strategies=trend_continuation"
        "&min_score=65"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["data_rows"] == n - 1
    assert data["metrics"]["initial_equity"] == 10000
    assert data["assumptions"]["fee_rate_per_side"] == 0.001
    assert data["assumptions"]["slippage_bps_per_side"] == 5.0
    assert data["assumptions"]["min_score"] == 65
    assert data["strategies"] == ["trend_continuation"]
    assert isinstance(data["recent_trades"], list)



def test_web_scan_can_use_dynamic_universe(monkeypatch):
    captured = {}

    class FakeUniverse:
        def __init__(self, **kwargs):
            captured["universe_init"] = kwargs

        def select(self, config):
            captured["universe_config"] = config
            return UniverseSelection(
                candidates=(
                    MarketCandidate(
                        symbol="BTC/USDT",
                        base="BTC",
                        quote="USDT",
                        market_type="spot",
                        active=True,
                        quote_volume=50_000_000,
                        last=100_000,
                        bid=99_990,
                        ask=100_010,
                        spread_bps=2.0,
                    ),
                    MarketCandidate(
                        symbol="ETH/USDT",
                        base="ETH",
                        quote="USDT",
                        market_type="spot",
                        active=True,
                        quote_volume=25_000_000,
                        last=3_000,
                        bid=2_999,
                        ask=3_001,
                        spread_bps=6.7,
                    ),
                ),
                total_markets=300,
                eligible_markets=120,
            )

    class FakeScanner:
        def __init__(self, **kwargs):
            captured["scanner_init"] = kwargs

        def scan(self, symbols):
            captured["symbols"] = symbols
            assessment = SimpleNamespace(
                status=SimpleNamespace(value="FORMING"),
                signal=SimpleNamespace(value="LONG"),
                score=65,
                reason="Test setup",
                strategy="breakout_retest",
            )
            return [
                SimpleNamespace(
                    symbol=symbol,
                    assessment=assessment,
                    score=65,
                    structure=None,
                    setup=None,
                    data_quality=None,
                    error=None,
                )
                for symbol in symbols
            ]

    monkeypatch.setattr(web, "DynamicMarketUniverse", FakeUniverse)
    monkeypatch.setattr(web, "MarketScanner", FakeScanner)

    response = client.get(
        "/api/scan",
        params={
            "exchange": "kucoin",
            "market_type": "spot",
            "auto_universe": "true",
            "quote_currencies": "USDT",
            "max_markets": 2,
            "min_quote_volume": 1_000_000,
            "timeframe": "15m",
            "confirm_timeframes": "4h",
            "strategies": "breakout_retest",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auto_universe"] is True
    assert body["universe"]["total_markets"] == 300
    assert captured["symbols"] == ["BTC/USDT", "ETH/USDT"]
    assert captured["universe_config"].max_markets == 2
    assert captured["scanner_init"]["confirmation_timeframes"] == ("4h",)
    assert captured["scanner_init"]["strategy_names"] == (
        "breakout_retest",
    )
