import pandas as pd

from trading_bot.exchanges.ccxt_client import get_public_client


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

    df = pd.DataFrame(candles, columns=OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True,
    )

    return (
        df.sort_values("timestamp", kind="stable")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )


def get_ticker(
    symbol: str,
    exchange_id: str = "kraken",
    market_type: str = "spot",
):
    client = get_public_client(
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
    since: int | None = None,
) -> pd.DataFrame:
    client = get_public_client(
        exchange_id=exchange_id,
        market_type=market_type,
    )
    candles = client.fetch_ohlcv(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        since=since,
    )
    # Preserve upstream defects so the scanner can reject them. Historical
    # pagination uses normalize_ohlcv separately to merge overlapping batches.
    if not candles:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    frame = pd.DataFrame(candles, columns=OHLCV_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame
