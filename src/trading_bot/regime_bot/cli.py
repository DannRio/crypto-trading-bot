from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from .backtest import RegimeBacktester
from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .models import MarketSignal, to_dict
from .service import LiveMarketRequest, RegimeBotService, parse_timeframes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-regime",
        description=(
            "Independent market-regime signal bot. Classifies bullish/bearish "
            "conditions and emits risk-defined conditional entry and exit levels."
        ),
    )
    parser.add_argument("--config", default="", help="Optional JSON configuration file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze one live market snapshot.")
    _add_live_arguments(analyze)
    analyze.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    watch = subparsers.add_parser("watch", help="Continuously refresh a live market signal.")
    _add_live_arguments(watch)
    watch.add_argument("--interval-seconds", type=int, default=60)
    watch.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Number of refreshes; 0 runs until interrupted.",
    )
    watch.add_argument("--json", action="store_true")

    backtest = subparsers.add_parser(
        "backtest",
        help="Backtest the engine using closed-candle CSV files.",
    )
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--execution-timeframe", required=True)
    backtest.add_argument("--execution-csv", required=True)
    backtest.add_argument(
        "--context-csv",
        action="append",
        default=[],
        metavar="TIMEFRAME=PATH",
        help="Optional higher-timeframe CSV. Repeat for multiple timeframes.",
    )
    backtest.add_argument("--equity", type=float, default=10_000.0)
    backtest.add_argument("--risk-percent", type=float, default=None)
    backtest.add_argument("--maximum-notional-multiple", type=float, default=None)
    backtest.add_argument("--json", action="store_true")
    return parser


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--market-type", default="spot")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--execution-timeframe", default="15m")
    parser.add_argument(
        "--context-timeframes",
        default="1h,4h",
        help="Comma-separated context timeframes.",
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--maximum-staleness-bars",
        type=int,
        default=3,
        help="Reject live data older than this many candles; 0 disables the check.",
    )
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument("--maximum-notional-multiple", type=float, default=None)
    parser.add_argument(
        "--include-open-candle",
        action="store_true",
        help="Analyze the currently forming candle. Disabled by default for safety.",
    )


def _load_config(path: str) -> RegimeBotConfig:
    return RegimeBotConfig.from_json(path) if path else RegimeBotConfig()


def _live_request(args: argparse.Namespace) -> LiveMarketRequest:
    contexts = tuple(
        timeframe
        for timeframe in parse_timeframes(args.context_timeframes)
        if timeframe != args.execution_timeframe.strip().lower()
    )
    return LiveMarketRequest(
        exchange_id=args.exchange,
        market_type=args.market_type,
        symbol=args.symbol,
        execution_timeframe=args.execution_timeframe.strip().lower(),
        context_timeframes=contexts,
        limit=args.limit,
        closed_candles_only=not args.include_open_candle,
        maximum_staleness_bars=args.maximum_staleness_bars,
    )


def _print_signal(signal: MarketSignal, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(to_dict(signal), indent=2, sort_keys=True))
        return

    print(
        f"{signal.symbol} | {signal.regime.value.upper()} | "
        f"{signal.action.value.upper()} | score {signal.score:.1f}/100 | "
        f"confidence {signal.confidence:.1f}/100"
    )
    print(
        f"Decision candle: {signal.generated_at.isoformat()} | "
        f"timeframe consensus: {signal.consensus * 100:.0f}%"
    )
    for state in signal.timeframe_states:
        print(
            f"  {state.timeframe:>5}: {state.regime.value:<15} "
            f"score {state.score:>6.1f} | ADX {state.adx:>5.1f} | "
            f"RSI {state.rsi:>5.1f} | ATR {state.atr_percent * 100:>5.2f}%"
        )
    if signal.trigger_price is not None:
        print(f"Trigger: {signal.trigger_price:,.8f}")
    if signal.entry_plan:
        plan = signal.entry_plan
        print(
            f"Entry zone: {plan.entry_low:,.8f} - {plan.entry_high:,.8f} | "
            f"reference {plan.reference_entry:,.8f}"
        )
        print(
            f"Stop: {plan.stop_loss:,.8f} | TP1: {plan.take_profit_1:,.8f} | "
            f"TP2: {plan.take_profit_2:,.8f} | TP3: {plan.take_profit_3:,.8f}"
        )
        print(
            f"Size: {plan.position_size:,.8f} | notional {plan.notional_value:,.2f} | "
            f"planned risk {plan.actual_risk_amount:,.2f}"
        )
    for reason in signal.reasons:
        print(f"Reason: {reason}")
    for warning in signal.warnings:
        print(f"Warning: {warning}")
    print(f"Fingerprint: {signal.fingerprint}")


def _read_csv(path: str) -> pd.DataFrame:
    frame = pd.read_csv(Path(path))
    if "timestamp" not in frame.columns:
        raise ValueError(f"CSV {path!r} must contain a timestamp column.")
    return frame


def _parse_context_csv(values: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--context-csv must use TIMEFRAME=PATH format.")
        timeframe, path = value.split("=", 1)
        normalized = timeframe.strip().lower()
        if not normalized or not path.strip():
            raise ValueError("--context-csv must use TIMEFRAME=PATH format.")
        frames[normalized] = _read_csv(path.strip())
    return frames


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        engine = RegimeSignalEngine(config)

        if args.command in {"analyze", "watch"}:
            service = RegimeBotService(engine)
            request = _live_request(args)
            if args.command == "analyze":
                signal = service.analyze_live(
                    request,
                    equity=args.equity,
                    risk_percent=args.risk_percent,
                    maximum_notional_multiple=args.maximum_notional_multiple,
                )
                _print_signal(signal, as_json=args.json)
                return 0

            if args.interval_seconds < 60:
                raise ValueError("watch interval must be at least 60 seconds.")
            if args.iterations < 0:
                raise ValueError("iterations cannot be negative.")
            iteration = 0
            previous_fingerprint = ""
            while args.iterations == 0 or iteration < args.iterations:
                signal = service.analyze_live(
                    request,
                    equity=args.equity,
                    risk_percent=args.risk_percent,
                    maximum_notional_multiple=args.maximum_notional_multiple,
                )
                if signal.fingerprint != previous_fingerprint:
                    _print_signal(signal, as_json=args.json)
                    previous_fingerprint = signal.fingerprint
                iteration += 1
                if args.iterations and iteration >= args.iterations:
                    break
                time.sleep(args.interval_seconds)
            return 0

        frames = {args.execution_timeframe.strip().lower(): _read_csv(args.execution_csv)}
        frames.update(_parse_context_csv(args.context_csv))
        result = RegimeBacktester(engine).run(
            symbol=args.symbol,
            frames=frames,
            execution_timeframe=args.execution_timeframe,
            starting_equity=args.equity,
            risk_percent=args.risk_percent,
            maximum_notional_multiple=args.maximum_notional_multiple,
        )
        if args.json:
            print(json.dumps(to_dict(result), indent=2, sort_keys=True))
        else:
            print(
                f"{result.symbol} {result.execution_timeframe} | "
                f"trades {result.trades} | return {result.total_return_percent:.2f}% | "
                f"max drawdown {result.max_drawdown_percent:.2f}%"
            )
            profit_factor = (
                "infinite (no losing trades)"
                if result.profit_factor is None
                else f"{result.profit_factor:.3f}"
            )
            print(
                f"Win rate {result.win_rate_percent:.2f}% | "
                f"profit factor {profit_factor} | "
                f"expectancy {result.expectancy_r:.3f}R"
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # CLI boundary: present a concise operational error.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
