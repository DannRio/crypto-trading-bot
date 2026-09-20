import pandas as pd

from trading_bot.analysis.indicators import (
    add_atr,
    add_ema,
    add_rsi,
)


def sample_data() -> pd.DataFrame:
    prices = [100 + i for i in range(30)]

    return pd.DataFrame(
        {
            "open": prices,
            "high": [price + 2 for price in prices],
            "low": [price - 2 for price in prices],
            "close": prices,
            "volume": [1000] * 30,
        }
    )


def test_ema_is_added():
    df = sample_data()

    result = add_ema(
        df,
        periods=(5,),
    )

    assert "ema_5" in result.columns
    assert result["ema_5"].notna().all()


def test_rsi_is_added():
    df = sample_data()

    result = add_rsi(
        df,
        period=14,
    )

    assert "rsi_14" in result.columns
    assert result["rsi_14"].iloc[-1] > 50


def test_atr_is_added():
    df = sample_data()

    result = add_atr(
        df,
        period=14,
    )

    assert "atr_14" in result.columns
    assert result["atr_14"].iloc[-1] > 0