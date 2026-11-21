"""Independent market-regime signal engine."""

from .backtest import RegimeBacktester
from .config import IndicatorConfig, RegimeBotConfig, RiskConfig, SignalConfig
from .engine import RegimeSignalEngine
from .models import (
    BacktestResult,
    BacktestTrade,
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
    TimeframeState,
    to_dict,
)

__all__ = [
    "BacktestResult",
    "BacktestTrade",
    "EntryPlan",
    "IndicatorConfig",
    "MarketRegime",
    "MarketSignal",
    "PositionSide",
    "RegimeBacktester",
    "RegimeBotConfig",
    "RegimeSignalEngine",
    "RiskConfig",
    "SetupType",
    "SignalAction",
    "SignalConfig",
    "TimeframeState",
    "to_dict",
]
