from collections.abc import Callable

import pandas as pd

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import SetupStatus
from trading_bot.strategies.breakout_retest import evaluate_breakout_retest
from trading_bot.strategies.models import SetupAssessment
from trading_bot.strategies.range_mean_reversion import (
    evaluate_range_mean_reversion,
)
from trading_bot.strategies.trend_continuation import (
    evaluate_trend_continuation,
)
from trading_bot.strategies.trend_continuation_v2 import evaluate_trend_continuation_v2


StrategyEvaluator = Callable[
    [pd.DataFrame, MarketStructure, int, int],
    SetupAssessment,
]

DEFAULT_STRATEGIES = ("trend_continuation",)

STRATEGY_EVALUATORS: dict[str, StrategyEvaluator] = {
    "trend_continuation": evaluate_trend_continuation,
    "trend_continuation_v2": evaluate_trend_continuation_v2,
    "breakout_retest": evaluate_breakout_retest,
    "range_mean_reversion": evaluate_range_mean_reversion,
}


def available_strategies() -> tuple[str, ...]:
    return tuple(STRATEGY_EVALUATORS)


def normalize_strategy_names(
    names: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    values = tuple(
        str(name).strip()
        for name in (names or DEFAULT_STRATEGIES)
        if str(name).strip()
    )
    if not values:
        raise ValueError("At least one strategy is required.")

    if len(set(values)) != len(values):
        raise ValueError("Strategy names must be unique.")

    unknown = [
        name
        for name in values
        if name not in STRATEGY_EVALUATORS
    ]
    if unknown:
        raise ValueError(
            "Unknown strategy: " + ", ".join(unknown)
        )
    return values


def evaluate_strategies(
    df: pd.DataFrame,
    structure: MarketStructure,
    strategy_names: tuple[str, ...] | list[str] | None = None,
    min_confirmed_score: int = 70,
    min_forming_score: int = 50,
    strategy_parameters: dict[str, dict] | None = None,
) -> tuple[SetupAssessment, ...]:
    names = normalize_strategy_names(strategy_names)
    parameters = strategy_parameters or {}
    unknown_parameter_groups = set(parameters).difference(
        STRATEGY_EVALUATORS
    )
    if unknown_parameter_groups:
        raise ValueError(
            "Unknown strategy parameter group: "
            + ", ".join(sorted(unknown_parameter_groups))
        )

    assessments = []

    for name in names:
        try:
            assessment = STRATEGY_EVALUATORS[name](
                df,
                structure,
                min_confirmed_score=min_confirmed_score,
                min_forming_score=min_forming_score,
                **parameters.get(name, {}),
            )
        except TypeError as exc:
            raise ValueError(
                f"Invalid parameters for strategy {name}: {exc}"
            ) from exc
        if assessment.strategy != name:
            assessment = SetupAssessment(
                signal=assessment.signal,
                status=assessment.status,
                score=assessment.score,
                reason=assessment.reason,
                reasons=assessment.reasons,
                strategy=name,
            )
        assessments.append(assessment)

    return tuple(assessments)


def select_best_assessment(
    assessments: tuple[SetupAssessment, ...],
) -> SetupAssessment:
    if not assessments:
        raise ValueError("At least one strategy assessment is required.")

    status_rank = {
        SetupStatus.CONFIRMED: 2,
        SetupStatus.FORMING: 1,
        SetupStatus.NO_TRADE: 0,
    }

    return max(
        assessments,
        key=lambda item: (
            status_rank[item.status],
            item.score,
        ),
    )
