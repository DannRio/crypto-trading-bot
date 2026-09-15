import ccxt
import pandas as pd


def create_exchange():
    return ccxt.kraken({
        "enableRateLimit": True
    })


def get_ticker(symbol):
    exchange = create_exchange()
    return exchange.fetch_ticker(symbol)


def get_ohlcv(symbol, timeframe="1h", limit=200):
    exchange = create_exchange()

    candles = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )

    df = pd.DataFrame(
        candles,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True
    )

    return df