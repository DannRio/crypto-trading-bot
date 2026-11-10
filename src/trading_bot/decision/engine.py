from dataclasses import replace

import pandas as pd

from trading_bot.analysis import analyze_market_structure
from trading_bot.core.enums import SetupStatus, Signal
from trading_bot.decision.models import DecisionResult
from trading_bot.planning import TradePlanConfig, create_trade_setup
from trading_bot.strategies import (
    DEFAULT_STRATEGIES,
    evaluate_strategies,
    normalize_strategy_names,
    select_best_assessment,
)


class DecisionEngine:
    """Single strategy-selection path used by scanner and research."""

    def __init__(
        self,
        account_equity: float = 10_000,
        risk_percent: float = 0.01,
        min_confirmed_score: int = 70,
        min_forming_score: int = 50,
        strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
        strategy_parameters: dict[str, dict] | None = None,
        trade_plan_config: TradePlanConfig | None = None,
        strategy_plan_configs: dict[str, TradePlanConfig] | None = None,
        allow_short: bool = True,
    ):
        self.allow_short = allow_short
        self.account_equity = account_equity
        self.risk_percent = risk_percent
        self.min_confirmed_score = min_confirmed_score
        self.min_forming_score = min_forming_score
        self.strategy_names = normalize_strategy_names(
            strategy_names
        )
        self.strategy_parameters = strategy_parameters or {}
        self.trade_plan_config = (
            trade_plan_config or TradePlanConfig()
        )
        self.strategy_plan_configs = strategy_plan_configs or {}

    def evaluate(self, data: pd.DataFrame) -> DecisionResult:
        structure = analyze_market_structure(data)
        candidates = evaluate_strategies(
            data,
            structure,
            strategy_names=self.strategy_names,
            min_confirmed_score=self.min_confirmed_score,
            min_forming_score=self.min_forming_score,
            strategy_parameters=self.strategy_parameters,
        )
        if not self.allow_short:
            candidates = tuple(
                replace(
                    item, signal=Signal.NO_TRADE, status=SetupStatus.NO_TRADE,
                    reason=item.reason + "; SHORT unavailable for this market.",
                ) if item.signal == Signal.SHORT else item
                for item in candidates
            )
        assessment = select_best_assessment(candidates)

        setup = None
        if assessment.status == SetupStatus.CONFIRMED:
            latest = data.iloc[-1]
            setup = create_trade_setup(
                signal=assessment.signal,
                status=assessment.status,
                score=assessment.score,
                reasons=assessment.reasons,
                entry_price=float(latest["close"]),
                atr=float(latest["atr_14"]),
                structure=structure,
                account_equity=self.account_equity,
                risk_percent=self.risk_percent,
                config=self.strategy_plan_configs.get(
                    assessment.strategy, self.trade_plan_config
                ),
            )

        return DecisionResult(
            assessment=assessment,
            structure=structure,
            setup=setup,
            candidates=candidates,
        )
