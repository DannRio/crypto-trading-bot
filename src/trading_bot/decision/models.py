from dataclasses import dataclass

from trading_bot.analysis import MarketStructure
from trading_bot.planning import TradeSetup
from trading_bot.strategies import SetupAssessment


@dataclass(frozen=True)
class DecisionResult:
    assessment: SetupAssessment
    structure: MarketStructure
    setup: TradeSetup | None
    candidates: tuple[SetupAssessment, ...] = ()
