import pandas as pd

from trading_bot.core.enums import SetupStatus
from trading_bot.scanner import MarketScanner


FREQUENCIES = {
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
}


def trending_candles(
    timeframe: str,
    direction: str,
    rows: int = 270,
) -> pd.DataFrame:
    step = 0.5 if direction == "up" else -0.5
    start = 100.0 if direction == "up" else 300.0
    prices = [start + i * step for i in range(rows)]

    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01",
                periods=rows,
                freq=FREQUENCIES[timeframe],
                tz="UTC",
            ),
            "open": prices,
            "high": [price + 1.0 for price in prices],
            "low": [price - 1.0 for price in prices],
            "close": prices,
            "volume": [1200.0] * rows,
        }
    )


def test_aligned_higher_timeframes_preserve_confirmed_setup():
    datasets = {
        "15m": trending_candles("15m", "up"),
        "1h": trending_candles("1h", "up"),
        "4h": trending_candles("4h", "up"),
    }

    scanner = MarketScanner(
        timeframe="15m",
        confirmation_timeframes=("1h", "4h"),
        min_confirmed_score=50,
        data_fetcher=lambda timeframe, **kwargs: datasets[
            timeframe
        ].copy(),
    )

    result = scanner.scan_symbol("BTC/USDT")

    assert result.assessment is not None
    assert result.assessment.status == SetupStatus.CONFIRMED
    assert result.setup is not None
    assert result.multi_timeframe_aligned is True
    assert [item.aligned for item in result.confirmations] == [
        True,
        True,
    ]


def test_opposite_higher_timeframe_invalidates_setup():
    datasets = {
        "15m": trending_candles("15m", "up"),
        "4h": trending_candles("4h", "down"),
    }

    scanner = MarketScanner(
        timeframe="15m",
        confirmation_timeframes=("4h",),
        min_confirmed_score=50,
        data_fetcher=lambda timeframe, **kwargs: datasets[
            timeframe
        ].copy(),
    )

    result = scanner.scan_symbol("BTC/USDT")

    assert result.primary_assessment is not None
    assert (
        result.primary_assessment.status
        == SetupStatus.CONFIRMED
    )
    assert result.assessment is not None
    assert result.assessment.status == SetupStatus.NO_TRADE
    assert result.setup is None
    assert result.multi_timeframe_aligned is False
    assert result.confirmations[0].aligned is False


def test_confirmation_timeframe_must_be_higher_than_primary():
    try:
        MarketScanner(
            timeframe="1h",
            confirmation_timeframes=("15m",),
        )
    except ValueError as exc:
        assert "must be higher" in str(exc)
    else:
        raise AssertionError("Expected invalid timeframe hierarchy.")
