import pandas as pd

from trading_bot.exchanges.ccxt_client import CCXTClient


OHLCV_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


def normalize_ohlcv(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    df = pd.DataFrame(
        candles,
        columns=OHLCV_COLUMNS,
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True,
    )

    df = (
        df.sort_values("timestamp")
        .drop_duplicates(
            subset=["timestamp"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    return df


def get_ticker(
    symbol: str,
    exchange_id: str = "kraken",
    market_type: str = "spot",
):
    client = CCXTClient(
        exchange_id=exchange_id,
        market_type=market_type,
    )

    return client.fetch_ticker(symbol)


def get_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 500,
    exchange_id: str = "kraken",
    market_type: str = "spot",
) -> pd.DataFrame:
    client = CCXTClient(
        exchange_id=exchange_id,
        market_type=market_type,
    )

    candles = client.fetch_ohlcv(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )

    return normalize_ohlcv(candles)