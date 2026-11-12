"""Public-data, conditional coin analysis; never an order-execution service."""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from trading_bot.analysis import decision_indicators, analyze_market_structure
from trading_bot.analysis.fundamentals import FundamentalService
from trading_bot.analysis.journal import AnalysisJournal
from trading_bot.analysis.markets import assess_liquidity, data_exchange_id, resolve_market
from trading_bot.analysis.preflight import public_market_snapshot
from trading_bot.config import INDICATOR_WINDOW, OPERATOR_STRATEGIES, operator_policy
from trading_bot.data import get_ohlcv, validate_ohlcv
from trading_bot.data.validation import latest_expected_close
from trading_bot.exchanges import get_public_client
from trading_bot.scanner import MarketScanner
from trading_bot.planning import TradePlanConfig
from trading_bot.planning.validation import revalidate_trade_plan


ENGINE_VERSION = "coin-analysis-v2.0"
STRATEGIES = OPERATOR_STRATEGIES
EXCHANGES = {"kucoin", "kraken", "binance", "bybit", "okx", "coinbase"}


def json_safe(value):
    """Produce strict JSON without trusting provider-specific scalar types."""
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if hasattr(value, "item"):
        return json_safe(value.item())
    return None


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _utc(value):
    parsed = pd.Timestamp(value)
    if pd.isna(parsed) or parsed.tzinfo is None:
        raise ValueError("A timezone-aware analysis clock is required.")
    return parsed.tz_convert("UTC").to_pydatetime()


def default_journal_path():
    path = Path(os.getenv("TRADING_ANALYSIS_DB", "data/analysis/reports.db")).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


@dataclass(frozen=True)
class AnalysisRequest:
    query: str
    exchange: str = "kucoin"
    market_type: str = "spot"
    quote: str = "USDT"
    equity: float = 10_000
    risk_percent: float = 0.25
    notional: float = 1_000
    fee_rate: float = 0.001
    slippage_bps: float = 5
    include_fundamentals: bool = False

    def __post_init__(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 120 or not self.query.isprintable():
            raise ValueError("Enter a printable coin name, ticker, or pair of 1–120 characters.")
        if self.exchange not in EXCHANGES:
            raise ValueError("Unsupported exchange for coin analysis.")
        if self.market_type not in {"spot", "swap"}:
            raise ValueError("Choose spot or perpetual swap.")
        if self.quote not in {"USD", "USDT", "USDC"}:
            raise ValueError("This profile supports USD, USDT, or USDC quotes.")
        bounds = {"equity": (1, 1e9), "risk_percent": (0.01, 5),
                  "notional": (1, 1e8), "fee_rate": (0, 0.05), "slippage_bps": (0, 500)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if not _finite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and between {low:g} and {high:g}.")
        if not isinstance(self.include_fundamentals, bool):
            raise ValueError("include_fundamentals must be a boolean.")


class CoinAnalysisService:
    """Coordinates existing strategies, explicit evidence gaps, and an audit record.

    ``READY`` means the configured research rules currently pass. It is neither
    a calibrated probability nor authorization to execute a trade. All providers
    are injectable so failure modes can be checked without live exchange access.
    """

    def __init__(self, journal=None, fundamentals=None, client_factory=None,
                 data_fetcher=None, scanner_factory=None, resolver=None, now=None):
        self.journal = journal if journal is not None else AnalysisJournal(default_journal_path())
        self.fundamentals = fundamentals if fundamentals is not None else FundamentalService()
        self.client_factory = client_factory or get_public_client
        self.data_fetcher = data_fetcher or get_ohlcv
        self.scanner_factory = scanner_factory or MarketScanner
        self.resolver = resolver or resolve_market
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _clock(self):
        return _utc(self.now())

    def analyze(self, request: AnalysisRequest):
        started = self._clock()
        policy = operator_policy(equity=request.equity, risk_percent=request.risk_percent,
                                 notional=request.notional, fee_rate=request.fee_rate,
                                 slippage_bps=request.slippage_bps)
        report = {
            "report_id": str(uuid4()), "engine_version": ENGINE_VERSION,
            "started_at": started.isoformat(), "generated_at": started.isoformat(),
            "expires_at": None, "instrument": None, "resolution": None,
            "decision": {"action": "INSUFFICIENT_DATA", "bias": "NEUTRAL",
                         "summary": "A verified instrument and complete inputs are required.",
                         "reasons": [], "counter_evidence": [], "blockers": [],
                         "score": None, "win_probability": None, "validated": False,
                         "manual_review_required": False},
            "technical": {}, "charts": [], "plan": None,
            "liquidity": {"status": "unavailable", "reasons": []},
            "market_context": {"status": "unavailable"},
            "derivatives": {"status": "not_applicable"},
            "fundamentals": {"status": "unavailable", "metrics": {}, "sources": [],
                             "events": [], "coverage_gaps": [], "review_required": False},
            "warnings": ["Research rules are unvalidated for profitability; the score is not a win probability.",
                         "This report does not submit orders or monitor a position after entry.",
                         "Historical tests do not reproduce live order-book, listing or news evidence."],
            "policy": policy,
            "inputs": {"request": asdict(request), "strategies": list(STRATEGIES),
                       "policy": policy,
                       "timeframes": {"context": "4h", "setup": "1h", "entry": "15m"},
                       "candles": {}, "market_snapshots": {}},
        }
        venue = data_exchange_id(request.exchange, request.market_type)
        try:
            client = self.client_factory(venue, request.market_type)
            resolution = self.resolver(request.query, request.exchange, request.market_type,
                                       request.quote, client=client)
            if not isinstance(resolution, dict):
                raise ValueError("Invalid market resolution.")
        except Exception:
            resolution = {"status": "unavailable", "candidates": [], "warnings": [],
                          "reasons": ["Exchange metadata could not be retrieved."]}
        report["resolution"] = json_safe(resolution)
        report["warnings"].extend(resolution.get("warnings", []))
        if resolution.get("status") != "resolved":
            report["decision"]["blockers"] = resolution.get("reasons", ["Select an exact instrument."])
            return self._finish(report)

        market = resolution.get("market")
        if (not isinstance(market, dict) or not all(isinstance(market.get(key), str) and market[key]
                for key in ("symbol", "base", "quote"))
                or market["quote"] != request.quote
                or (market.get("type") is not None and market["type"] != request.market_type)
                or resolution.get("data_exchange_id", venue) != venue):
            report["decision"]["blockers"].append("Resolved metadata does not identify the requested market.")
            return self._finish(report)
        symbol = market["symbol"]
        report["instrument"] = {
            "symbol": symbol, "name": resolution.get("coin_name") or market["base"],
            "base": market["base"], "quote": market["quote"], "exchange": request.exchange,
            "data_exchange_id": venue, "market_type": request.market_type,
            "contract_size": market.get("contractSize"), "short_supported": resolution.get("short_supported", False),
            "account_eligibility_verified": False,
        }
        if request.include_fundamentals:
            try:
                evidence = self.fundamentals.collect(
                    base=market["base"], name=resolution.get("coin_name"), now=self._clock())
                if not isinstance(evidence, dict):
                    raise ValueError("Malformed fundamental evidence.")
                report["fundamentals"] = json_safe(evidence)
            except Exception:
                report["fundamentals"]["coverage_gaps"] = ["Fundamental providers could not be reached."]
        else:
            report["fundamentals"].update(status="disabled", coverage_gaps=[
                "Optional fundamental and event context was not requested."])
        report["fundamentals"]["decision_role"] = "optional_manual_context"
        if request.include_fundamentals and request.exchange != "kucoin":
            report["warnings"].append(
                "Event coverage is limited to KuCoin announcements; notices from the selected exchange were not checked.")

        try:
            scanner = self.scanner_factory(
                exchange_id=venue, market_type=request.market_type, timeframe="1h",
                confirmation_timeframes=("4h",), entry_timeframe="15m", candle_limit=500,
                account_equity=request.equity, risk_percent=request.risk_percent / 100,
                min_confirmed_score=70, strategy_names=STRATEGIES,
                strategy_plan_configs={name: TradePlanConfig(
                    max_notional_value=request.notional, max_notional_percent=1.0,
                    min_target_net_rr=1.0, min_blended_net_rr=1.0,
                ) for name in STRATEGIES},
                data_fetcher=self.data_fetcher, now=lambda: pd.Timestamp(self._clock()),
                fee_rate=request.fee_rate, slippage_bps=request.slippage_bps,
            )
            scan = scanner.scan_symbol(symbol)
            if (scan.symbol != symbol or scan.exchange_id != venue
                    or scan.market_type != request.market_type or scan.timeframe != "1h"):
                raise ValueError("The scanner returned a different market or timeframe.")
            frames = scanner.decision_frames()
            self._technical(report, scan, frames)
        except Exception:
            report["decision"]["blockers"].append("Technical analysis could not be completed.")
            return self._finish(report)
        as_of = report["technical"].get("as_of")
        report["market_context"] = self._market_context(client, market, venue, request.market_type, frames, as_of)
        if request.market_type == "swap":
            report["derivatives"] = self._derivatives(client, symbol)
            report["warnings"].append(
                "Contract size is checked, but account eligibility, leverage, margin, liquidation and future funding costs are not modeled.")

        # Fetch executable-side evidence last, after slower project/context queries.
        snapshot = public_market_snapshot(client, symbol, self._clock)
        ticker, book = snapshot["ticker"], snapshot["order_book"]
        report["warnings"].extend(snapshot["warnings"])
        now = self._clock()
        liquidity = assess_liquidity(market, ticker, book, request.notional, now)
        report["liquidity"] = liquidity
        plan, plan_errors = self._plan(json_safe(scan.setup), request, liquidity, market)
        if plan and liquidity.get("best_bid") and liquidity.get("best_ask"):
            midpoint = (liquidity["best_bid"] + liquidity["best_ask"]) / 2
            checked = max(request.notional, plan["position_size"] * midpoint)
            if checked > request.notional:
                liquidity = assess_liquidity(market, ticker, book, checked, self._clock())
                report["liquidity"] = liquidity
                plan, plan_errors = self._plan(json_safe(scan.setup), request, liquidity, market)
        report["plan"] = plan
        report["warnings"].extend(liquidity.get("warnings", []))
        report["inputs"]["market_snapshots"] = {
            "ticker": {k: ticker.get(k) for k in ("symbol", "timestamp", "datetime", "_received_at",
                                                    "bid", "ask", "last", "quoteVolume", "baseVolume", "percentage", "_quote_volume_snapshot")},
            "order_book": {k: book.get(k) for k in ("symbol", "timestamp", "datetime", "_received_at", "bids", "asks", "nonce")},
        }
        self._decision(report, scan, plan_errors, request)
        for chart in report["charts"]:
            if plan:
                chart["levels"].update({key: plan[key] for key in (
                    "entry_low", "entry_high", "stop_loss", "take_profit_1", "take_profit_2")})
        return self._finish(report)

    def _technical(self, report, scan, frames):
        assessment, structure = json_safe(scan.assessment), json_safe(scan.structure)
        as_of = (_utc(scan.signal_time) + timedelta(hours=1)).isoformat() if scan.signal_time is not None else None
        technical = {"as_of": as_of, "assessment": assessment, "structure": structure,
                     "data_quality": json_safe(scan.data_quality), "indicators": {},
                     "confirmations": json_safe(scan.confirmations), "entry_trigger": json_safe(scan.entry_trigger),
                     "multi_timeframe_aligned": scan.multi_timeframe_aligned,
                     "patterns": [{"name": item.strategy, "status": json_safe(item.status),
                                   "reason": item.reason, "event_id": item.event_id,
                                   "event_time": item.event_time} for item in scan.candidates],
                     "error": "Candle quality or strategy evaluation failed." if scan.error else None,
                     "frame_errors": [],
                     "chart_basis": "Validated, closed candles frozen at the 1h decision close; live quotes are separate."}
        if as_of is None or pd.Timestamp(as_of) != latest_expected_close(self._clock(), "1h"):
            technical["frame_errors"].append("The 1h decision close is missing, stale, or in the future.")
        for timeframe in ("4h", "1h", "15m"):
            frame = frames.get(timeframe)
            if frame is None or frame.empty:
                technical["frame_errors"].append(f"Validated {timeframe} candles are unavailable.")
                continue
            quality = validate_ohlcv(frame, timeframe=timeframe, min_rows=INDICATOR_WINDOW)
            times = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            delta = pd.Timedelta(timeframe.replace("m", "min"))
            if (not quality.valid or quality.missing_intervals or as_of is None
                    or (times + delta > pd.Timestamp(as_of)).any()
                    or times.iloc[-1] + delta != latest_expected_close(as_of, timeframe)):
                technical["frame_errors"].append(f"{timeframe} candles are invalid or not current at the frozen decision close.")
                continue
            frame = frame.tail(INDICATOR_WINDOW).copy()
            data = decision_indicators(frame)
            columns = ["timestamp", "open", "high", "low", "close", "volume"]
            report["inputs"]["candles"][timeframe] = json_safe(frame[columns].to_dict("records"))
            report["charts"].append({"timeframe": timeframe, "as_of": as_of,
                "candles": json_safe(data.tail(180)[columns + ["ema_50", "ema_200"]].to_dict("records")),
                "levels": {"support": (structure or {}).get("support"), "resistance": (structure or {}).get("resistance")},
                "level_timeframe": "1h"})
            if timeframe == "1h":
                latest = data.iloc[-1]
                technical["indicators"] = json_safe({"close": latest["close"], "ema_50": latest["ema_50"],
                    "ema_200": latest["ema_200"], "rsi": latest["rsi_14"], "atr": latest["atr_14"],
                    "volume_ratio": (structure or {}).get("volume_ratio")})
        report["technical"] = technical

    def _market_context(self, client, market, venue, market_type, frames, as_of):
        unavailable = {"status": "unavailable", "reason": "A verified BTC benchmark could not be compared."}
        if not as_of or "1h" not in frames:
            return unavailable
        try:
            if market["base"] == "BTC":
                benchmark, symbol = frames["1h"], market["symbol"]
            else:
                candidates = [m for m in client.load_markets().values() if m.get("base") == "BTC"
                    and m.get("quote") == market["quote"] and m.get("type") == market_type
                    and m.get("active") is True and (market_type == "spot" or
                    (m.get("linear") is True and m.get("settle") == market["quote"]))]
                if len(candidates) != 1:
                    return unavailable
                symbol = candidates[0]["symbol"]
                raw = self.data_fetcher(symbol=symbol, timeframe="1h", limit=500, exchange_id=venue, market_type=market_type)
                times = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")
                if times.isna().any():
                    return unavailable
                benchmark = raw.loc[times + pd.Timedelta(hours=1) <= pd.Timestamp(as_of)].copy()
                benchmark["timestamp"] = times.loc[benchmark.index]
            quality = validate_ohlcv(benchmark, timeframe="1h", min_rows=INDICATOR_WINDOW)
            if not quality.valid or quality.missing_intervals or benchmark.iloc[-1]["timestamp"] + pd.Timedelta(hours=1) != pd.Timestamp(as_of):
                return unavailable
            benchmark = benchmark.tail(INDICATOR_WINDOW).copy()
            asset = frames["1h"].set_index("timestamp")["close"]
            btc = benchmark.set_index("timestamp")["close"]
            joined = pd.concat([asset.rename("asset"), btc.rename("benchmark")], axis=1).dropna()
            if len(joined) < 25:
                return unavailable
            changes = (joined.iloc[-1] / joined.iloc[-25] - 1) * 100
            returns = joined.pct_change().dropna().tail(100)
            correlation = returns["asset"].corr(returns["benchmark"]) if len(returns) >= 50 and (returns.std() > 0).all() else None
            return json_safe({"status": "ok", "benchmark": symbol, "as_of": as_of,
                "asset_return_24h_pct": changes["asset"], "benchmark_return_24h_pct": changes["benchmark"],
                "relative_return_24h_pp": changes["asset"] - changes["benchmark"],
                "correlation_1h_100": correlation,
                "benchmark_regime": analyze_market_structure(decision_indicators(benchmark)).regime,
                "interpretation": "Recent relative performance and correlation are context, not forecasts.",
                "candles": benchmark[["timestamp", "open", "high", "low", "close", "volume"]].to_dict("records")})
        except Exception:
            return unavailable

    def _derivatives(self, client, symbol):
        result = {"status": "partial", "funding": None, "open_interest": None,
                  "coverage_gaps": [], "costs_included_in_plan": False}
        for method, key, fields in (
            ("fetch_funding_rate", "funding", ("fundingRate", "fundingTimestamp", "nextFundingTimestamp", "markPrice", "indexPrice")),
            ("fetch_open_interest", "open_interest", ("openInterestAmount", "openInterestValue")),
        ):
            try:
                raw = getattr(client, method)(symbol)
                if not isinstance(raw, dict) or raw.get("symbol", symbol) != symbol or not any(_finite(raw.get(f)) for f in fields):
                    raise ValueError("Incomplete derivative snapshot.")
                result[key] = json_safe({**{f: raw.get(f) for f in fields}, "symbol": symbol,
                    "source_timestamp": raw.get("timestamp"), "retrieved_at": self._clock(),
                    "freshness": "Provider observation time may be unavailable; no funding forecast is inferred."})
            except Exception:
                result["coverage_gaps"].append(f"Current {key.replace('_', ' ')} is unavailable.")
        result["coverage_gaps"].append("Future funding, margin and liquidation are not modeled.")
        return result

    def _plan(self, proposed, request, liquidity, market):
        return revalidate_trade_plan(
            proposed, account_equity=request.equity, risk_percent=request.risk_percent / 100,
            notional_cap=request.notional, fee_rate=request.fee_rate,
            slippage_bps=request.slippage_bps, market_type=request.market_type,
            liquidity=liquidity, market=market, min_target_net_rr=1, min_blended_net_rr=1,
        )

    def _decision(self, report, scan, plan_errors, request):
        decision, technical, liquidity = report["decision"], report["technical"], report["liquidity"]
        assessment = technical.get("assessment") or {}
        structure = technical.get("structure") or {}
        signal, status = assessment.get("signal"), assessment.get("status")
        decision.update(score=scan.score if assessment and not scan.error else None,
                        reasons=list(assessment.get("reasons") or []))
        if assessment.get("reason"):
            decision["reasons"].insert(0, assessment["reason"])
        decision["bias"] = signal if signal in {"LONG", "SHORT"} else {
            "BULL_TREND": "LONG", "BEAR_TREND": "SHORT"}.get(structure.get("regime"), "NEUTRAL")
        directional = signal in {"LONG", "SHORT"}
        data_errors, hard_blocks, wait_blocks = [], list(plan_errors), []
        primary_quality = technical.get("data_quality") or {}
        if (scan.error or primary_quality.get("valid") is not True
                or primary_quality.get("missing_intervals") != 0):
            data_errors.append("Complete, contiguous and current closed candles are unavailable.")
        indicators = technical.get("indicators") or {}
        required_indicators = ("close", "ema_50", "ema_200", "rsi", "atr", "volume_ratio")
        if (not all(_finite(indicators.get(key)) for key in required_indicators)
                or any(indicators.get(key, 0) <= 0 for key in ("close", "ema_50", "ema_200", "atr"))
                or not 0 <= indicators.get("rsi", -1) <= 100 or indicators.get("volume_ratio", -1) < 0):
            data_errors.append("Required 1h indicators are missing, non-finite, or invalid.")
        data_errors.extend(technical.get("frame_errors", []))
        if not _finite(scan.score) or not 0 <= scan.score <= 100:
            data_errors.append("The strategy's rule-alignment score is invalid.")
        elif directional and scan.score < 70:
            wait_blocks.append("The strategy does not meet the minimum rule-alignment score of 70.")
        confirmations = technical.get("confirmations") or []
        if len(confirmations) != 1 or not isinstance(confirmations[0], dict) or confirmations[0].get("timeframe") != "4h":
            data_errors.append("The required 4h confirmation is missing or ambiguous.")
        else:
            confirmation = confirmations[0]
            quality = confirmation.get("data_quality") or {}
            if (confirmation.get("error") or confirmation.get("regime") is None
                    or quality.get("valid") is not True or quality.get("missing_intervals") != 0):
                data_errors.append("4h confirmation data is unavailable or invalid.")
            elif directional and confirmation.get("aligned") is not True:
                reason = "4h context does not confirm this setup."
                decision["counter_evidence"].append(reason)
                (hard_blocks if confirmation.get("aligned") is False else wait_blocks).append(reason)
        if directional and technical.get("multi_timeframe_aligned") is not True:
            wait_blocks.append("Higher-timeframe alignment has not been independently confirmed.")
        trigger = technical.get("entry_trigger") or {}
        quality = trigger.get("data_quality") or {}
        if (trigger.get("timeframe") != "15m" or trigger.get("error") or trigger.get("regime") is None
                or quality.get("valid") is not True or quality.get("missing_intervals") != 0):
            data_errors.append("15m entry confirmation data is unavailable or invalid.")
        elif directional and trigger.get("confirmed") is not True:
            reason = trigger.get("reason") or "15m entry confirmation is absent."
            decision["counter_evidence"].append(reason)
            wait_blocks.append(reason)
        if liquidity.get("status") not in {"pass", "fail"}:
            data_errors.extend(liquidity.get("reasons") or ["Required liquidity evidence is unavailable."])
        if liquidity.get("status") == "fail":
            hard_blocks.extend(liquidity.get("reasons") or ["The market failed its liquidity checks."])
        if request.market_type == "spot" and decision["bias"] == "SHORT":
            hard_blocks.append("Spot cannot open a short position in this workflow.")
        if request.market_type == "swap" and not report["resolution"].get("short_supported"):
            hard_blocks.append("Only active linear, quote-settled perpetuals are supported.")
        fundamentals = report["fundamentals"]
        if request.include_fundamentals:
            if fundamentals.get("review_required"):
                decision["manual_review_required"] = True
                decision["counter_evidence"].append("A matching critical project or exchange event requires manual review.")
                report["warnings"].append(
                    "Critical event evidence is manual context. The technical policy and its historical results do not evaluate this event.")
            now = self._clock()

            def recent_source(source, max_observation_age=None):
                try:
                    receipt_age = (now - _utc(source.get("retrieved_at"))).total_seconds()
                    if not -5 <= receipt_age <= 900:
                        return False
                    if max_observation_age is not None:
                        age = (now - _utc(source.get("as_of"))).total_seconds()
                        return -300 <= age <= max_observation_age
                    return True
                except (TypeError, ValueError, OverflowError):
                    return False

            sources = [source for source in (fundamentals.get("sources") or []) if isinstance(source, dict)]
            source_ok = any(source.get("provider") == "CoinGecko" and source.get("status") == "ok"
                            and recent_source(source, 86400) for source in sources)
            identity = fundamentals.get("identity") or {}
            if (not isinstance(identity, dict) or identity.get("symbol") != report["instrument"]["base"]
                    or not identity.get("coingecko_id") or not source_ok):
                report["warnings"].append("Fresh, identity-verified optional project evidence is unavailable.")
            if not any(source.get("provider") == "KuCoin announcements"
                       and source.get("status") in {"ok", "partial"} and recent_source(source) for source in sources):
                report["warnings"].append("Optional KuCoin announcement context is unavailable or stale.")
        decision["counter_evidence"].extend(fundamentals.get("risk_flags", []))
        report["warnings"].extend(fundamentals.get("coverage_gaps", []))
        plan = report["plan"]
        if plan:
            if plan.get("signal") != signal:
                hard_blocks.append("The trade plan does not match the assessed direction.")
            side = "buy_fill" if plan["signal"] == "LONG" else "sell_fill"
            price = (liquidity.get(side) or {}).get("vwap")
            if _finite(price):
                direction = 1 if plan["signal"] == "LONG" else -1
                cancel = plan.get("cancel_price")
                if direction * (price - plan["stop_loss"]) <= 0 or direction * (price - plan["take_profit_1"]) >= 0 or (
                    _finite(cancel) and direction * (price - cancel) <= 0):
                    hard_blocks.append("Price has already reached invalidation or the first target; this entry is no longer valid.")
                elif not plan["entry_low"] <= price <= plan["entry_high"]:
                    wait_blocks.append("The current executable-side price is outside the planned entry zone.")
                plan["snapshot_entry_vwap"] = price
            else:
                data_errors.append("An executable-side price estimate is unavailable.")
        elif directional and status == "CONFIRMED" and not plan_errors:
            hard_blocks.append("The confirmed assessment did not produce a valid trade plan.")
        decision["blockers"] = data_errors + hard_blocks + wait_blocks
        if data_errors:
            action, summary = "INSUFFICIENT_DATA", "A required market or technical input is missing, stale, or invalid."
        elif hard_blocks:
            action, summary = "NO_TRADE", "The proposed trade fails an instrument, liquidity, risk, or price-invalidation gate."
        elif wait_blocks:
            action, summary = "WAIT", "The technical view needs fresh evidence or an acceptable entry before it can qualify."
        elif plan and status == "CONFIRMED" and signal in {"LONG", "SHORT"}:
            action, summary = signal + "_READY", "The research rules qualify a conditional entry at this snapshot; profitability is unvalidated."
        elif status == "FORMING":
            action, summary = "WAIT", "A setup is forming; context or entry confirmation is still incomplete."
        else:
            action, summary = "NO_TRADE", "None of the configured strategies currently supplies a complete trade setup."
        decision.update(action=action, summary=summary)

    def _finish(self, report):
        now = self._clock()
        deadline = now + timedelta(minutes=15)
        as_of = report["technical"].get("as_of")
        if as_of:
            next_close = _utc(as_of) + timedelta(hours=1)
            deadline = min(deadline, next_close)
            if next_close <= now:
                report["decision"].update(action="INSUFFICIENT_DATA", summary="A new 1h candle closed during analysis; refresh the report.")
                report["decision"]["blockers"].append("The technical snapshot crossed its next decision boundary.")
        if report["decision"]["action"].endswith("_READY"):
            snapshots = report["liquidity"].get("freshness", {})
            for label in dict.fromkeys(("ticker", "order_book", *snapshots)):
                stamp = (snapshots.get(label) or {}).get("timestamp")
                try:
                    observed = _utc(stamp) if stamp else None
                    age = (now - observed).total_seconds() if observed else None
                except (TypeError, ValueError, OverflowError):
                    observed, age = None, None
                if age is None or not -5 <= age < 120:
                    report["decision"].update(action="INSUFFICIENT_DATA", summary="Market snapshots expired during analysis; refresh the report.")
                    report["decision"]["blockers"].append("Final quote/order-book freshness check failed.")
                    break
                deadline = min(deadline, observed + timedelta(seconds=120))
        report.update(generated_at=now.isoformat(), expires_at=deadline.isoformat())
        if report["plan"]:
            report["plan"].update(expires_at=deadline.isoformat(), usable_now=report["decision"]["action"].endswith("_READY"))
        report["warnings"] = list(dict.fromkeys(report["warnings"]))
        report = json_safe(report)
        report["inputs_sha256"] = hashlib.sha256(json.dumps(report["inputs"], sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        # Persistence is a required gate: never return an apparently saved READY
        # report after a database failure, and never replace an earlier outcome.
        self.journal.save(report)
        saved = self.journal.get(report["report_id"])
        if saved is None:
            raise RuntimeError("Analysis report persistence could not be verified.")
        return saved
