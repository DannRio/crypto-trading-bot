"""Offline API and navigation tests for strategy diagnostics."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.data import HistoricalDataStore
from trading_bot.web import app as web

client = TestClient(web.app)


def fixtures():
    candles=320
    times=pd.date_range("2023-01-01", periods=candles, freq="h", tz="UTC")
    frame=pd.DataFrame({
        "timestamp":times,
        "open":np.full(candles,100.0),
        "high":np.full(candles,101.0),
        "low":np.full(candles,99.0),
        "close":np.full(candles,100.0),
        "volume":np.full(candles,1000.0),
    })
    return frame


def test_diagnostics_page_exists_and_is_navigable():
    root=client.get("/")
    page=client.get("/diagnostics")
    assert root.status_code==200
    assert 'href="/diagnostics"' in root.text
    assert page.status_code==200
    for label in ["Annual marked-equity returns","Equity curve",
                  "Setup-score buckets","Market regimes at entry",
                  "Trade-level analysis","Export filtered CSV"]:
        assert label in page.text
    assert "https://cdn" not in page.text


def test_diagnostics_uses_saved_archive_and_single_shared_backtest(monkeypatch,tmp_path):
    data=fixtures()
    store=HistoricalDataStore(tmp_path)
    store.upsert("kucoin","spot","BTC/USDT","1h",data)
    monkeypatch.setattr(web,"HistoricalDataStore",lambda:store)
    monkeypatch.setattr(web,"get_ohlcv",lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("Diagnostics may not contact exchange")
    ))
    original=web.run_decision_backtest
    calls=[]

    def run_once(*args,**kwargs):
        calls.append(kwargs)
        return original(*args,**kwargs)

    monkeypatch.setattr(web,"run_decision_backtest",run_once)
    result=client.get("/api/diagnostics",params={
        "exchange":"kucoin","market_type":"spot",
        "symbol":"BTC/USDT","timeframe":"1h",
        "min_score":80,"risk_percent":1.5,
        "equity":12500,"fee_rate":0.001,"slippage_bps":5,
    })
    assert result.status_code==200,result.text
    out=result.json()
    assert out["live_execution"] is False
    assert out["dataset"]["rows"]==320
    assert len(calls)==1
    assert calls[0]["allow_short"] is False
    assert calls[0]["risk_percent"]==0.015
    assert calls[0]["min_confirmed_score"]==80
    assert out["summary"]["final_equity"]==out["equity_curve"][-1]["equity"]
    assert "yearly" in out and "trades" in out and "regimes" in out


def test_diagnostics_missing_data_is_clear_error(monkeypatch,tmp_path):
    monkeypatch.setattr(web,"HistoricalDataStore",
                        lambda:HistoricalDataStore(tmp_path))
    response=client.get("/api/diagnostics",params={
        "exchange":"kucoin","market_type":"spot",
        "symbol":"BTC/USDT","timeframe":"1h",
    })
    assert response.status_code==422
    assert "No saved OHLCV" in response.json()["detail"]


def test_diagnostics_rejects_invalid_type_and_risk():
    response=client.get("/api/diagnostics",params={
        "exchange":"kraken","market_type":"swap"
    })
    assert response.status_code==422
    assert "krakenfutures" in response.json()["detail"]
    response=client.get("/api/diagnostics?risk_percent=10")
    assert response.status_code==422


def test_diagnostics_rejects_unbounded_archives(monkeypatch):
    data=fixtures().iloc[:1].copy()
    huge=pd.concat([data]*150001,ignore_index=True)
    monkeypatch.setattr(
        web,"HistoricalDataStore",
        lambda:SimpleNamespace(load=lambda *args:huge)
    )
    response=client.get("/api/diagnostics")
    assert response.status_code==422
    assert "150,000" in response.json()["detail"]
