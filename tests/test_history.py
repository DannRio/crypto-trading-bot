import pandas as pd

from trading_bot.data import HistoricalDataStore, fetch_ohlcv_range


class FakeClient:
    def __init__(self, candles):
        self.candles = candles

    def fetch_ohlcv(
        self,
        symbol,
        timeframe="1h",
        limit=500,
        since=None,
        params=None,
    ):
        eligible = [
            candle
            for candle in self.candles
            if since is None or candle[0] >= since
        ]
        return eligible[:limit]


def make_candles(count=6):
    start = pd.Timestamp("2026-01-01", tz="UTC")
    rows = []
    for i in range(count):
        timestamp = int(
            (start + pd.Timedelta(hours=i)).timestamp() * 1000
        )
        price = 100.0 + i
        rows.append(
            [
                timestamp,
                price,
                price + 1,
                price - 1,
                price + 0.5,
                1000.0,
            ]
        )
    return rows


def test_fetch_ohlcv_range_paginates():
    candles = make_candles()
    client = FakeClient(candles)

    result = fetch_ohlcv_range(
        symbol="BTC/USDT",
        start="2026-01-01",
        end="2026-01-01 06:00:00+00:00",
        timeframe="1h",
        batch_limit=2,
        client=client,
    )

    assert len(result) == 6
    assert result["timestamp"].is_unique
    assert result["timestamp"].is_monotonic_increasing


def test_historical_store_upserts_without_duplicates(tmp_path):
    store = HistoricalDataStore(tmp_path)
    data = pd.DataFrame(
        make_candles(3),
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )
    data["timestamp"] = pd.to_datetime(
        data["timestamp"],
        unit="ms",
        utc=True,
    )

    store.upsert("kucoin", "spot", "BTC/USDT", "1h", data)
    store.upsert("kucoin", "spot", "BTC/USDT", "1h", data)

    loaded = store.load("kucoin", "spot", "BTC/USDT", "1h")

    assert len(loaded) == 3
    assert loaded["timestamp"].is_unique
