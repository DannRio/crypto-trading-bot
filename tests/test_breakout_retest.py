import pandas as pd

from trading_bot.analysis import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies import evaluate_breakout_retest


def test_breakout_retest_can_confirm_long_setup():
    rows = []
    for i in range(30):
        close = 105.0
        high = 110.0
        low = 100.0
        volume = 1000.0

        if 25 <= i <= 28:
            close = 112.0
            high = 113.0
            low = 109.0
            volume = 1500.0

        if i == 29:
            close = 111.0
            high = 112.0
            low = 109.8
            volume = 1200.0

        rows.append(
            {
                "open": close - 0.5,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "rsi_14": 60.0,
                "atr_14": 2.0,
                "volume_sma_20": 1000.0,
            }
        )

    structure = MarketStructure(
        regime=MarketRegime.BULL_TREND,
        support=100.0,
        resistance=110.0,
        atr_pct=0.018,
        volume_ratio=1.2,
        higher_high=True,
        higher_low=True,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )

    result = evaluate_breakout_retest(
        pd.DataFrame(rows),
        structure,
    )

    assert result.signal == Signal.LONG
    assert result.status == SetupStatus.CONFIRMED
    assert result.strategy == "breakout_retest"
    assert result.score >= 70
