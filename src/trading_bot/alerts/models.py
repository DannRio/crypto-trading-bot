from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class AlertKind(str, Enum):
    SETUP_CONFIRMED = "SETUP_CONFIRMED"
    SETUP_FORMING = "SETUP_FORMING"
    SCAN_ERROR = "SCAN_ERROR"


@dataclass(frozen=True)
class AlertEvent:
    kind: AlertKind
    created_at: pd.Timestamp
    symbol: str
    timeframe: str
    message: str
    signal: str = ""
    status: str = ""
    score: int = 0
    strategy: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return "|".join(
            [
                self.kind.value,
                self.symbol,
                self.timeframe,
                self.signal,
                self.status,
                self.strategy,
            ]
        )
