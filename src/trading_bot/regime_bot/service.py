from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

import pandas as pd

from .engine import RegimeSignalEngine
from .models import MarketSignal, SignalAction
from .validation import drop_incomplete_candle, timeframe_to_timedelta


@dataclass(frozen=True)
class LiveMarketRequest:
    exchange_id: str
    market_type: str
    symbol: str
    execution_timeframe: str
    context_timeframes: tuple[str, ...] = ()
    limit: int = 500
    closed_candles_only: bool = True
    maximum_staleness_bars: int = 3

    @property
    def timeframes(self) -> tuple[str, ...]:
        values = [self.execution_timeframe, *self.context_timeframes]
        unique: list[str] = []
        for value in values:
            normalized = value.strip().lower()
            timeframe_to_timedelta(normalized)
            if normalized not in unique:
                unique.append(normalized)
        return tuple(unique)


class RegimeBotService:
    def __init__(self, engine: RegimeSignalEngine | None = None):
        self.engine = engine or RegimeSignalEngine()

    def fetch_frames(self, request: LiveMarketRequest) -> dict[str, pd.DataFrame]:
        minimum_limit = self.engine.config.minimum_bars + (
            1 if request.closed_candles_only else 0
        )
        if request.limit < minimum_limit:
            raise ValueError(
                f"limit must be at least {minimum_limit} candles for this mode."
            )
        if request.maximum_staleness_bars < 0:
            raise ValueError("maximum_staleness_bars cannot be negative.")

        # Imported lazily so the deterministic core can be used without CCXT.
        from trading_bot.data.market_data import normalize_ohlcv
        from trading_bot.exchanges.ccxt_client import CCXTClient

        client = CCXTClient(
            exchange_id=request.exchange_id,
            market_type=request.market_type,
        )
        markets = client.load_markets()
        if request.symbol not in markets:
            raise ValueError(
                f"{request.symbol!r} is not available on {request.exchange_id} "
                f"for market type {request.market_type!r}."
            )

        exchange_timeframes = getattr(client.exchange, "timeframes", None) or {}
        frames: dict[str, pd.DataFrame] = {}
        for timeframe in request.timeframes:
            if exchange_timeframes and timeframe not in exchange_timeframes:
                raise ValueError(
                    f"{request.exchange_id} does not advertise timeframe {timeframe!r}."
                )
            candles = client.fetch_ohlcv(
                symbol=request.symbol,
                timeframe=timeframe,
                limit=request.limit,
            )
            frame = normalize_ohlcv(candles)
            if request.closed_candles_only:
                frame = drop_incomplete_candle(frame, timeframe)
            if frame.empty:
                raise ValueError(
                    f"{request.exchange_id} returned no usable closed candles for "
                    f"{request.symbol} {timeframe}."
                )
            if request.maximum_staleness_bars:
                duration = timeframe_to_timedelta(timeframe)
                latest_close = frame.iloc[-1]["timestamp"] + duration
                now = pd.Timestamp.now(tz="UTC")
                if now - latest_close > duration * request.maximum_staleness_bars:
                    raise ValueError(
                        f"Latest {timeframe} candle is stale by more than "
                        f"{request.maximum_staleness_bars} bars."
                    )
            frames[timeframe] = frame
        return frames

    def analyze_live(
        self,
        request: LiveMarketRequest,
        *,
        equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
    ) -> MarketSignal:
        frames = self.fetch_frames(request)
        signal = self.engine.analyze(
            symbol=request.symbol,
            frames=frames,
            execution_timeframe=request.execution_timeframe,
            equity=equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
        )
        if request.market_type.lower() == "spot" and signal.action in {
            SignalAction.SHORT,
            SignalAction.WATCH_SHORT,
        }:
            signal = replace(
                signal,
                warnings=signal.warnings
                + (
                    "Bearish/short signal is informational on a spot market; "
                    "verify margin or derivatives eligibility before execution.",
                ),
            )
        return signal


def parse_timeframes(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    normalized: list[str] = []
    for item in items:
        timeframe = str(item).strip().lower()
        if not timeframe:
            continue
        timeframe_to_timedelta(timeframe)
        if timeframe not in normalized:
            normalized.append(timeframe)
    return tuple(normalized)
