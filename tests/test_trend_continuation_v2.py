import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies.trend_continuation_v2 import evaluate_trend_continuation_v2


def structure():
    return MarketStructure(
        regime=MarketRegime.BULL_TREND, support=90, resistance=110,
        atr_pct=.02, volume_ratio=1.4, higher_high=True,
        higher_low=True, lower_high=False, lower_low=False,
        high_volatility=False,
    )


def test_v2_rejects_permanent_bullish_state_without_event():
    df = pd.DataFrame([
        {"open":105,"high":107,"low":104,"close":106,"ema_50":103,"ema_200":95,"rsi_14":55,"atr_14":2},
        {"open":106,"high":108,"low":105,"close":107,"ema_50":103,"ema_200":95,"rsi_14":56,"atr_14":2},
        {"open":106,"high":109,"low":105,"close":107,"ema_50":103,"ema_200":95,"rsi_14":57,"atr_14":2},
    ])
    result = evaluate_trend_continuation_v2(df, structure())
    assert result.status != SetupStatus.CONFIRMED


def test_v2_confirms_fresh_volume_breakout():
    df = pd.DataFrame([
        {"open":106,"high":109,"low":105,"close":108,"ema_50":104,"ema_200":95,"rsi_14":48,"atr_14":2},
        {"open":108,"high":110,"low":107,"close":109,"ema_50":104,"ema_200":95,"rsi_14":49,"atr_14":2},
        {"open":109,"high":112,"low":108,"close":111,"ema_50":105,"ema_200":96,"rsi_14":55,"atr_14":2},
    ])
    result = evaluate_trend_continuation_v2(df, structure())
    assert result.signal == Signal.LONG
    assert result.status == SetupStatus.CONFIRMED
    assert result.strategy == "trend_continuation_v2"
