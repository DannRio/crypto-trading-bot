import pandas as pd

from trading_bot.analysis import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies import evaluate_range_mean_reversion


def test_range_mean_reversion_can_confirm_support_rejection():
    data = pd.DataFrame(
        [
            {
                "open": 101.0,
                "high": 103.0,
                "low": 100.2,
                "close": 102.0,
                "rsi_14": 35.0,
                "atr_14": 2.0,
            }
        ]
    )
    structure = MarketStructure(
        regime=MarketRegime.RANGE,
        support=100.0,
        resistance=120.0,
        atr_pct=0.02,
        volume_ratio=1.0,
        higher_high=False,
        higher_low=False,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )

    result = evaluate_range_mean_reversion(
        data,
        structure,
    )

    assert result.signal == Signal.LONG
    assert result.status == SetupStatus.CONFIRMED
    assert result.strategy == "range_mean_reversion"
    assert result.score >= 70
