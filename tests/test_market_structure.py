import pandas as pd

from trading_bot.analysis import add_indicators, analyze_market_structure
from trading_bot.core.enums import MarketRegime


def test_rising_market_is_bull_trend():
    prices = [100 + i * 0.5 for i in range(260)]
    volumes = [1000.0] * 259 + [1600.0]
    df = pd.DataFrame(
        {
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": volumes,
        }
    )
    data = add_indicators(df)
    structure = analyze_market_structure(data)

    assert structure.regime == MarketRegime.BULL_TREND
    assert structure.support < structure.resistance
    assert structure.higher_low
    assert structure.volume_ratio > 1.0
