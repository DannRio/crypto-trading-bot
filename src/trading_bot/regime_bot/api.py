from __future__ import annotations

import argparse
from typing import Any

import pandas as pd

from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .models import to_dict
from .service import LiveMarketRequest, RegimeBotService, parse_timeframes


def create_app(config: RegimeBotConfig | None = None):
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover - depends on optional install.
        raise RuntimeError('Install the web extra: pip install -e ".[web]"') from exc

    engine = RegimeSignalEngine(config)
    service = RegimeBotService(engine)
    app = FastAPI(
        title="Regime Signal Bot API",
        version="1.0.0",
        description=(
            "Signal-only API for bullish/bearish market classification and "
            "risk-defined entry/exit plans. Confidence is a rule score, not a probability."
        ),
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "engine": "regime_signal_bot",
            "minimum_bars": engine.config.minimum_bars,
        }

    @app.get("/v1/live-signal")
    def live_signal(
        symbol: str = Query(...),
        exchange: str = Query("kraken"),
        market_type: str = Query("spot"),
        execution_timeframe: str = Query("15m"),
        context_timeframes: str = Query("1h,4h"),
        limit: int = Query(500, ge=1, le=5000),
        maximum_staleness_bars: int = Query(3, ge=0, le=100),
        equity: float = Query(10_000.0, gt=0),
        risk_percent: float | None = Query(None, gt=0),
        maximum_notional_multiple: float | None = Query(None, gt=0),
        include_open_candle: bool = Query(False),
    ) -> dict[str, Any]:
        try:
            contexts = tuple(
                value
                for value in parse_timeframes(context_timeframes)
                if value != execution_timeframe.strip().lower()
            )
            request = LiveMarketRequest(
                exchange_id=exchange,
                market_type=market_type,
                symbol=symbol,
                execution_timeframe=execution_timeframe,
                context_timeframes=contexts,
                limit=limit,
                closed_candles_only=not include_open_candle,
                maximum_staleness_bars=maximum_staleness_bars,
            )
            signal = service.analyze_live(
                request,
                equity=equity,
                risk_percent=risk_percent,
                maximum_notional_multiple=maximum_notional_multiple,
            )
            return to_dict(signal)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/analyze")
    def analyze(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            symbol = str(payload["symbol"])
            execution_timeframe = str(payload["execution_timeframe"])
            raw_frames = payload["frames"]
            if not isinstance(raw_frames, dict) or not raw_frames:
                raise ValueError("frames must be a non-empty timeframe-to-candles object.")
            frames = {
                str(timeframe).strip().lower(): pd.DataFrame(candles)
                for timeframe, candles in raw_frames.items()
            }
            signal = engine.analyze(
                symbol=symbol,
                frames=frames,
                execution_timeframe=execution_timeframe,
                equity=float(payload.get("equity", 10_000.0)),
                risk_percent=(
                    float(payload["risk_percent"])
                    if payload.get("risk_percent") is not None
                    else None
                ),
                maximum_notional_multiple=(
                    float(payload["maximum_notional_multiple"])
                    if payload.get("maximum_notional_multiple") is not None
                    else None
                ),
            )
            return to_dict(signal)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto-regime-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--config", default="")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional install.
        raise RuntimeError('Install the web extra: pip install -e ".[web]"') from exc

    config = RegimeBotConfig.from_json(args.config) if args.config else RegimeBotConfig()
    uvicorn.run(create_app(config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
