import pandas as pd

from trading_bot.data.market_data import (
    OHLCV_COLUMNS,
    normalize_ohlcv,
)


def sample_candles():
    return [
        [
            1700000000000,
            100.0,
            110.0,
            95.0,
            105.0,
            1000.0,
        ],
        [
            1700003600000,
            105.0,
            115.0,
            102.0,
            112.0,
            1200.0,
        ],
    ]


def test_normalize_ohlcv_columns():
    df = normalize_ohlcv(
        sample_candles()
    )

    assert list(df.columns) == OHLCV_COLUMNS


def test_normalize_ohlcv_timestamp():
    df = normalize_ohlcv(
        sample_candles()
    )

    assert pd.api.types.is_datetime64_any_dtype(
        df["timestamp"]
    )

    assert str(
        df["timestamp"].dt.tz
    ) == "UTC"


def test_normalize_ohlcv_empty():
    df = normalize_ohlcv([])

    assert df.empty
    assert list(df.columns) == OHLCV_COLUMNS