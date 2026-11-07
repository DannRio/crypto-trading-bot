import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies import evaluate_trend_continuation


def test_bull_breakout_is_confirmed():
    df = pd.DataFrame(
        [
            {"close": 108.0, "ema_50": 104.0, "ema_200": 100.0, "rsi_14": 49.0, "atr_14": 2.0},
            {"close": 111.0, "ema_50": 105.0, "ema_200": 101.0, "rsi_14": 55.0, "atr_14": 2.0},
        ]
    )
    structure = MarketStructure(
        regime=MarketRegime.BULL_TREND,
        support=100.0,
        resistance=110.0,
        atr_pct=0.018,
        volume_ratio=1.3,
        higher_high=True,
        higher_low=True,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )
    result = evaluate_trend_continuation(df, structure)
    assert result.signal == Signal.LONG
    assert result.status == SetupStatus.CONFIRMED
    assert result.score >= 70


def test_range_market_is_no_trade():
    df = pd.DataFrame(
        [
            {"close": 100.0, "ema_50": 100.0, "ema_200": 100.0, "rsi_14": 50.0, "atr_14": 2.0},
            {"close": 101.0, "ema_50": 100.0, "ema_200": 100.5, "rsi_14": 52.0, "atr_14": 2.0},
        ]
    )
    structure = MarketStructure(
        regime=MarketRegime.RANGE,
        support=95.0,
        resistance=105.0,
        atr_pct=0.02,
        volume_ratio=1.0,
        higher_high=False,
        higher_low=False,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )
    result = evaluate_trend_continuation(df, structure)
    assert result.signal == Signal.NO_TRADE
    assert result.status == SetupStatus.NO_TRADE
