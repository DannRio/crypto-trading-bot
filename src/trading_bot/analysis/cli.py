"""Command-line access to a single, journaled market-analysis report."""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Sequence


def _finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise argparse.ArgumentTypeError("must be a finite number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a finite number")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-analyze",
        allow_abbrev=False,
        description="Analyze one crypto market using public data, save the research report to its journal, and print JSON. No orders are placed.",
        epilog='Examples: crypto-analyze QNT; crypto-analyze "Bitcoin Cash" --quote USDT; crypto-analyze BTC/USDT:USDT --market-type swap',
    )
    parser.add_argument("query", help="Coin name, ticker, or exact trading pair. Quote names containing spaces.")
    parser.add_argument("--exchange", default="kucoin", help="Exchange identifier (default: kucoin).")
    parser.add_argument("--market-type", choices=("spot", "swap"), default="spot",
                        help="Spot or perpetual swap market (default: spot).")
    parser.add_argument("--quote", default="USDT", help="Quote currency (default: USDT).")
    parser.add_argument("--equity", type=_finite_float, default=10_000,
                        help="Account equity in quote currency (default: 10000).")
    parser.add_argument("--risk-percent", type=_finite_float, default=0.25,
                        help="Risk budget as a percentage of equity; 0.25 means 0.25%% (default: 0.25).")
    parser.add_argument("--notional", type=_finite_float, default=1_000,
                        help="Order notional for the liquidity check, in quote currency (default: 1000).")
    parser.add_argument("--fee-rate", type=_finite_float, default=0.001,
                        help="Fee fraction per side; 0.001 means 0.1%% (default: 0.001).")
    parser.add_argument("--slippage-bps", type=_finite_float, default=5,
                        help="Assumed slippage per side in basis points (default: 5).")
    parser.add_argument("--no-fundamentals", action="store_true",
                        help="Disable fundamental and event providers for this request; the coverage gap remains explicit.")
    parser.add_argument("--journal", metavar="PATH",
                        help="SQLite journal path. Default: TRADING_ANALYSIS_DB or data/analysis/reports.db.")
    return parser


def _runtime_types():
    # Parsing --help and invalid numeric arguments should not initialize exchange
    # adapters or require the optional service runtime to import successfully.
    from trading_bot.analysis.journal import AnalysisJournal
    from trading_bot.analysis.service import AnalysisRequest, CoinAnalysisService
    return AnalysisRequest, CoinAnalysisService, AnalysisJournal


def _error(message: str) -> None:
    print(f"crypto-analyze: error: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request_type, service_type, journal_type = _runtime_types()
    except Exception:
        _error("Analysis runtime could not be loaded. Check the application installation and dependencies.")
        return 1
    try:
        request = request_type(
            query=args.query,
            exchange=args.exchange,
            market_type=args.market_type,
            quote=args.quote,
            equity=args.equity,
            risk_percent=args.risk_percent,
            notional=args.notional,
            fee_rate=args.fee_rate,
            slippage_bps=args.slippage_bps,
            include_fundamentals=not args.no_fundamentals,
        )
    except (ValueError, TypeError):
        # Validation errors must not echo untrusted input or exception details.
        _error("Invalid analysis inputs. Check the coin query, exchange, market, quote currency, and numeric limits.")
        return 2
    except Exception:
        _error("Analysis request could not be prepared.")
        return 1

    try:
        service = service_type(journal=journal_type(args.journal)) if args.journal is not None else service_type()
        report = service.analyze(request)
        if not isinstance(report, dict):
            raise ValueError("Invalid report shape")
        journal = report.get("journal")
        if isinstance(journal, dict) and journal.get("saved") is False:
            raise ValueError("Report was not journaled")
        # Prepare the entire payload before writing stdout. A malformed report
        # cannot produce half a document, NaN, Infinity, or a success message.
        encoded = json.dumps(report, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        _error("Analysis or journal persistence failed. Check provider availability and journal permissions; no valid report was printed.")
        return 1

    try:
        print(encoded)
    except (BrokenPipeError, OSError):
        _error("The report could not be written to standard output.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
