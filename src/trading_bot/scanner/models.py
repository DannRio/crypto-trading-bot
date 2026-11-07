from dataclasses import dataclass

from trading_bot.analysis import MarketStructure
from trading_bot.data import DataQualityReport
from trading_bot.planning import TradeSetup
from trading_bot.strategies import SetupAssessment


@dataclass(frozen=True)
class ScanResult:
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str
    assessment: SetupAssessment | None
    structure: MarketStructure | None
    setup: TradeSetup | None
    data_quality: DataQualityReport | None
    error: str | None = None

    @property
    def score(self) -> int:
        return self.assessment.score if self.assessment else 0
