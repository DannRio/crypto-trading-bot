from enum import Enum


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


class SetupStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    FORMING = "FORMING"
    NO_TRADE = "NO_TRADE"
