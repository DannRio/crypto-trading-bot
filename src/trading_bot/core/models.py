from dataclasses import dataclass

from trading_bot.core.enums import Signal


@dataclass(frozen=True)
class StrategyResult:
    signal: Signal
    reason: str