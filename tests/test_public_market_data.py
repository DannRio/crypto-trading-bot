"""Live candles must reach validation without silently repairing the feed."""
import pandas as pd
import pytest

from trading_bot.data import market_data
from trading_bot.data.validation import validate_ohlcv


def _row(hour, close=10):
    stamp = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=hour)
    return [int(stamp.timestamp() * 1000), 10, 11, 9, close, 1000]


def test_public_ohlcv_preserves_feed_order_and_duplicate_timestamps(monkeypatch):
    rows = [_row(2), _row(0), _row(0)]
    calls = []

    class PublicClient:
        def fetch_ohlcv(self, **kwargs):
            calls.append(kwargs)
            return rows

    def factory(**kwargs):
        assert kwargs == {"exchange_id": "fake", "market_type": "spot"}
        return PublicClient()

    monkeypatch.setattr(market_data, "get_public_client", factory)
    frame = market_data.get_ohlcv("SUI/USDT", timeframe="1h", limit=250,
                                 exchange_id="fake", since=123)
    assert len(frame) == 3
    assert frame["timestamp"].tolist() == [
        pd.to_datetime(row[0], unit="ms", utc=True) for row in rows
    ]
    assert calls == [{"symbol": "SUI/USDT", "timeframe": "1h", "limit": 250, "since": 123}]
    quality = validate_ohlcv(frame, timeframe="1h")
    assert not quality.valid
    assert quality.duplicate_timestamps == 1
    assert any("sorted" in issue for issue in quality.issues)


def test_public_ohlcv_does_not_hide_nonfinite_price_data(monkeypatch):
    class PublicClient:
        def fetch_ohlcv(self, **kwargs):
            return [_row(0), _row(1, close=float("nan"))]

    monkeypatch.setattr(market_data, "get_public_client", lambda **kwargs: PublicClient())
    frame = market_data.get_ohlcv("JUP/USDT", exchange_id="fake")
    assert len(frame) == 2
    assert pd.isna(frame.iloc[1]["close"])
    quality = validate_ohlcv(frame, timeframe="1h")
    assert not quality.valid
    assert any("non-finite" in issue for issue in quality.issues)


def test_public_ohlcv_rejects_malformed_timestamp(monkeypatch):
    class PublicClient:
        def fetch_ohlcv(self, **kwargs):
            row = _row(0)
            row[0] = "not-a-timestamp"
            return [row]

    monkeypatch.setattr(market_data, "get_public_client", lambda **kwargs: PublicClient())
    with pytest.raises(ValueError):
        market_data.get_ohlcv("SUI/USDT", exchange_id="fake")
