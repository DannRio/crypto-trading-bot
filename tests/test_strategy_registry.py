import pandas as pd
import pytest

from trading_bot.analysis import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.strategies import (
    available_strategies,
    evaluate_strategies,
    normalize_strategy_names,
    select_best_assessment,
)
from trading_bot.strategies.models import SetupAssessment


def assessment(strategy, status, score):
    return SetupAssessment(
        signal=Signal.LONG,
        status=status,
        score=score,
        reason="test",
        reasons=("test",),
        strategy=strategy,
    )


def test_registry_exposes_all_candidate_strategies():
    assert available_strategies() == (
        "trend_continuation",
        "trend_continuation_v2",
        "breakout_retest",
        "range_mean_reversion",
    )


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="Unknown strategy"):
        normalize_strategy_names(("not_real",))


def test_best_assessment_prefers_status_then_score():
    selected = select_best_assessment(
        (
            assessment(
                "trend_continuation",
                SetupStatus.FORMING,
                95,
            ),
            assessment(
                "breakout_retest",
                SetupStatus.CONFIRMED,
                72,
            ),
            assessment(
                "range_mean_reversion",
                SetupStatus.CONFIRMED,
                80,
            ),
        )
    )

    assert selected.strategy == "range_mean_reversion"
    assert selected.score == 80



def test_registry_applies_strategy_parameter_overrides():
    data = pd.DataFrame(
        [
            {
                "close": 101.0,
                "ema_50": 100.0,
                "ema_200": 90.0,
                "rsi_14": 55.0,
                "atr_14": 2.0,
            },
            {
                "close": 101.0,
                "ema_50": 100.0,
                "ema_200": 90.0,
                "rsi_14": 55.0,
                "atr_14": 2.0,
            },
        ]
    )
    structure = MarketStructure(
        regime=MarketRegime.BULL_TREND,
        support=95.0,
        resistance=110.0,
        atr_pct=0.02,
        volume_ratio=1.2,
        higher_high=False,
        higher_low=False,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )

    default = evaluate_strategies(
        data,
        structure,
        strategy_names=("trend_continuation",),
        min_confirmed_score=50,
    )[0]
    overridden = evaluate_strategies(
        data,
        structure,
        strategy_names=("trend_continuation",),
        min_confirmed_score=50,
        strategy_parameters={
            "trend_continuation": {
                "volume_ratio_threshold": 2.0
            }
        },
    )[0]

    assert default.score - overridden.score == 15
