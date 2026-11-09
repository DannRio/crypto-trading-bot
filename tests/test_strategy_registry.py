import pytest

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.strategies import (
    available_strategies,
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
