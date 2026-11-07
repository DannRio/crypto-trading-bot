from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetSpec:
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str
