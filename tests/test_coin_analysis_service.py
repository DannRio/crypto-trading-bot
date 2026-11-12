"""Deterministic service checks; every external provider is injected."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from trading_bot.analysis.journal import AnalysisJournal
from trading_bot.analysis.service import AnalysisRequest, CoinAnalysisService, default_journal_path
from trading_bot.scanner import MarketScanner


NOW = datetime(2026, 11, 12, 12, 5, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DECISION_CLOSE = pd.Timestamp("2026-11-12T12:00:00Z")
QUALITY = {"valid": True, "missing_intervals": 0, "row_count": 420}


def candles(timeframe):
    delta = pd.Timedelta(timeframe.replace("m", "min"))
    prices = [58.1 + 0.1 * index for index in range(420)]
    return pd.DataFrame({
        "timestamp": pd.date_range(end=DECISION_CLOSE - delta, periods=420, freq=delta),
        "open": prices, "high": [price + 0.5 for price in prices],
        "low": [price - 0.5 for price in prices], "close": prices,
        "volume": [1000.0] * 420,
    })


def instrument(market_type="spot"):
    market = {
        "symbol": "QNT/USDT", "id": "QNT-USDT", "base": "QNT", "baseId": "QNT", "quote": "USDT",
        "type": "spot", "spot": True, "contract": False, "active": True, "limits": {},
    }
    if market_type == "swap":
        market.update(symbol="QNT/USDT:USDT", id="QNTUSDTM", type="swap", spot=False,
                      swap=True, contract=True, linear=True, inverse=False, settle="USDT", contractSize=0.001)
    return market


def proposed_plan(signal="LONG"):
    return {
        "signal": signal, "status": "CONFIRMED", "score": 80,
        "reference_entry": 100.0, "entry_low": 99.5, "entry_high": 100.5,
        "stop_loss": 95.0 if signal == "LONG" else 105.0,
        "take_profit_1": 110.0 if signal == "LONG" else 90.0,
        "take_profit_2": 120.0 if signal == "LONG" else 80.0,
        "position_size": 20.0, "take_profit_1_fraction": 0.5,
        "move_stop_to_break_even_after_tp1": True, "break_even_mode": "cost",
    }


def confirmed_scan(market, signal="LONG"):
    regime = "BULL_TREND" if signal == "LONG" else "BEAR_TREND"
    return SimpleNamespace(
        symbol=market["symbol"], exchange_id="kucoinfutures" if market["type"] == "swap" else "kucoin",
        market_type=market["type"], timeframe="1h", score=80, error=None,
        signal_time=DECISION_CLOSE - pd.Timedelta(hours=1),
        assessment={"signal": signal, "status": "CONFIRMED", "score": 80,
                    "reason": "Synthetic confirmed strategy.", "reasons": ["Synthetic evidence."]},
        structure={"regime": regime, "support": 95.0, "resistance": 110.0, "volume_ratio": 1.2},
        data_quality=deepcopy(QUALITY), setup=proposed_plan(signal), candidates=(),
        confirmations=[{"timeframe": "4h", "regime": regime, "aligned": True,
                        "data_quality": deepcopy(QUALITY), "error": None}],
        multi_timeframe_aligned=True,
        entry_trigger={"timeframe": "15m", "regime": regime, "confirmed": True,
                       "data_quality": deepcopy(QUALITY), "error": None, "reason": "Synthetic trigger."},
    )


class PublicClient:
    def __init__(self, market):
        self.market = market
        self.exchange = SimpleNamespace(currencies={"QNT": {"name": "Quant"}})
        self.calls = []
        self.ticker = {"symbol": market["symbol"], "quoteVolume": 5_000_000,
                       "last": 100.0, "bid": 99.95, "ask": 100.05, "timestamp": NOW_MS - 1000}
        self.bulk_ticker = {"symbol": market["symbol"], "quoteVolume": 5_000_000, "last": 999.0}
        size = 1_000_000 if market["type"] == "swap" else 1000
        self.book = {"symbol": market["symbol"], "timestamp": NOW_MS - 1000,
                     "bids": [[99.95, size]], "asks": [[100.05, size]]}

    def load_markets(self, reload=False):
        return {self.market["symbol"]: self.market}

    def fetch_currencies(self):
        return self.exchange.currencies

    def fetch_ticker(self, symbol):
        self.calls.append(("fetch_ticker", symbol))
        return deepcopy(self.ticker)

    def fetch_tickers(self, symbols):
        self.calls.append(("fetch_tickers", symbols))
        return {self.market["symbol"]: deepcopy(self.bulk_ticker)}

    def fetch_order_book(self, symbol):
        self.calls.append(("fetch_order_book", symbol))
        return deepcopy(self.book)


class Fundamentals:
    def __init__(self):
        self.calls = 0
        self.report = {
            "status": "partial", "identity": {"symbol": "QNT", "coingecko_id": "quant-network"},
            "metrics": {"market_cap_usd": 1_000_000_000}, "events": [], "review_required": False,
            "sources": [
                {"provider": "CoinGecko", "status": "ok", "retrieved_at": NOW.isoformat(),
                 "as_of": (NOW - timedelta(minutes=10)).isoformat()},
                {"provider": "KuCoin announcements", "status": "partial", "retrieved_at": NOW.isoformat()},
            ],
            "risk_flags": [], "coverage_gaps": ["Comprehensive token-unlock coverage is unavailable."],
        }

    def collect(self, **kwargs):
        self.calls += 1
        return deepcopy(self.report)


class CoinAnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = AnalysisJournal(Path(self.directory.name) / "analysis.sqlite")
        self.market = instrument()
        self.client = PublicClient(self.market)
        self.scan = confirmed_scan(self.market)
        self.frames = {timeframe: candles(timeframe) for timeframe in ("4h", "1h", "15m")}
        self.fundamentals = Fundamentals()
        self.scanner_options = []
        self.client_options = []

    def make_service(self, **overrides):
        def client_factory(exchange, market_type):
            self.client_options.append((exchange, market_type))
            return self.client

        def scanner_factory(**options):
            self.scanner_options.append(options)
            return SimpleNamespace(scan_symbol=lambda symbol: deepcopy(self.scan),
                                   decision_frames=lambda: {key: value.copy(deep=True) for key, value in self.frames.items()})

        options = {"journal": self.journal, "fundamentals": self.fundamentals,
                   "client_factory": client_factory, "scanner_factory": scanner_factory,
                   "data_fetcher": lambda timeframe, **kwargs: self.frames[timeframe].copy(deep=True),
                   "now": lambda: NOW}
        options.update(overrides)
        return CoinAnalysisService(**options)

    def analyze(self, **request):
        return self.make_service().analyze(AnalysisRequest("Quant", **request))

    def use_swap(self, signal="SHORT"):
        self.market = instrument("swap")
        self.client = PublicClient(self.market)
        self.scan = confirmed_scan(self.market, signal)

    def test_ready_requires_complete_evidence_and_has_a_verified_expiring_record(self):
        report = self.analyze()
        self.assertEqual(report["decision"]["action"], "LONG_READY")
        self.assertTrue(report["plan"]["usable_now"])
        self.assertFalse(report["plan"]["execution_supported"])
        self.assertFalse(report["instrument"]["account_eligibility_verified"])
        self.assertEqual(report, self.journal.get(report["report_id"]))
        self.assertEqual(report["inputs_sha256"], sha256(json.dumps(
            report["inputs"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest())
        self.assertLessEqual(datetime.fromisoformat(report["expires_at"]), NOW + timedelta(seconds=119))
        self.assertLessEqual(report["plan"]["estimated_loss_at_stop"], 25.0 + 1e-10)
        self.assertEqual(report["plan"]["actual_risk_amount"], report["plan"]["estimated_loss_at_stop"])
        self.assertEqual(self.scanner_options[0]["confirmation_timeframes"], ("4h",))
        self.assertEqual(self.scanner_options[0]["entry_timeframe"], "15m")

    def test_missing_or_invalid_timeframe_evidence_cannot_be_ready(self):
        cases = (
            ("missing_4h", lambda: setattr(self.scan, "confirmations", [])),
            ("wrong_4h", lambda: self.scan.confirmations[0].update(timeframe="1d")),
            ("missing_15m", lambda: setattr(self.scan, "entry_trigger", None)),
            ("invalid_1h", lambda: self.scan.data_quality.update(valid=False)),
            ("gapped_1h", lambda: self.scan.data_quality.update(missing_intervals=1)),
            ("invalid_4h", lambda: self.scan.confirmations[0]["data_quality"].update(valid=False)),
            ("gapped_15m", lambda: self.scan.entry_trigger["data_quality"].update(missing_intervals=1)),
            ("nonfinite_indicator", lambda: self.scan.structure.update(volume_ratio=float("nan"))),
        )
        for label, invalidate in cases:
            self.scan = confirmed_scan(self.market)
            invalidate()
            with self.subTest(case=label):
                report = self.analyze()
                self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
                self.assertFalse(report["plan"]["usable_now"])

    def test_false_context_or_entry_trigger_does_not_trust_a_confirmed_status(self):
        self.scan.confirmations[0]["aligned"] = False
        self.assertEqual(self.analyze()["decision"]["action"], "NO_TRADE")
        self.scan = confirmed_scan(self.market)
        self.scan.entry_trigger["confirmed"] = False
        self.assertEqual(self.analyze()["decision"]["action"], "WAIT")
        self.scan = confirmed_scan(self.market)
        self.scan.multi_timeframe_aligned = None
        self.assertEqual(self.analyze()["decision"]["action"], "WAIT")
        self.scan = confirmed_scan(self.market)
        self.scan.score = 60
        self.assertEqual(self.analyze()["decision"]["action"], "WAIT")

    def test_missing_plan_or_direction_mismatch_cannot_be_ready(self):
        self.scan.setup = None
        self.assertEqual(self.analyze()["decision"]["action"], "NO_TRADE")
        self.scan.setup = proposed_plan("SHORT")
        self.assertEqual(self.analyze()["decision"]["action"], "NO_TRADE")

    def test_spot_bearish_thesis_is_not_an_actionable_short(self):
        self.scan = confirmed_scan(self.market, "SHORT")
        report = self.analyze()
        self.assertEqual(report["decision"]["bias"], "SHORT")
        self.assertEqual(report["decision"]["action"], "NO_TRADE")
        self.assertTrue(any("Spot cannot" in reason for reason in report["decision"]["blockers"]))

    def test_perpetual_short_uses_the_native_contract_and_adapter(self):
        self.use_swap()
        report = self.analyze(market_type="swap")
        self.assertEqual(report["decision"]["action"], "SHORT_READY")
        self.assertEqual(self.client_options, [("kucoinfutures", "swap")])
        self.assertEqual(report["instrument"]["symbol"], "QNT/USDT:USDT")
        self.assertAlmostEqual(report["plan"]["contract_count_estimate"], report["plan"]["position_size"] / 0.001)

    def test_contract_turnover_fallback_keeps_separate_time_and_price_provenance(self):
        self.use_swap()
        self.client.ticker["quoteVolume"] = None
        report = self.analyze(market_type="swap")
        self.assertEqual(report["decision"]["action"], "SHORT_READY")
        self.assertEqual([call[0] for call in self.client.calls], ["fetch_ticker", "fetch_tickers", "fetch_order_book"])
        snapshot = report["inputs"]["market_snapshots"]["ticker"]
        self.assertEqual(snapshot["last"], 100.0)
        self.assertEqual(snapshot["timestamp"], NOW_MS - 1000)
        self.assertIsNone(snapshot["quoteVolume"])
        self.assertEqual(snapshot["_quote_volume_snapshot"]["source_method"], "fetch_tickers")
        self.assertEqual(report["liquidity"]["quote_volume"], 5_000_000)
        self.assertEqual(report["liquidity"]["freshness"]["quote_volume"]["source"], "receipt")
        self.assertFalse(report["liquidity"]["quote_volume_source"]["observation_time_known"])

    def test_valid_low_or_zero_volume_is_not_overridden(self):
        for volume in (0, 100, "100"):
            self.client.calls.clear()
            self.client.ticker["quoteVolume"] = volume
            with self.subTest(volume=volume):
                report = self.analyze()
                self.assertEqual(report["decision"]["action"], "NO_TRADE")
                self.assertNotIn("fetch_tickers", [call[0] for call in self.client.calls])
                self.assertEqual(report["liquidity"]["quote_volume"], float(volume))

    def test_stale_fallback_volume_cannot_borrow_the_live_ticker_timestamp(self):
        self.client.ticker["quoteVolume"] = None
        self.client.bulk_ticker["timestamp"] = NOW_MS - 300_000
        report = self.analyze()
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertIn("Quote volume is stale.", report["liquidity"]["reasons"])

    def test_stale_or_wrong_instrument_books_are_not_tradeable(self):
        self.client.book["timestamp"] = NOW_MS - 300_000
        self.assertEqual(self.analyze()["decision"]["action"], "INSUFFICIENT_DATA")
        self.client.book["timestamp"] = NOW_MS
        self.client.book["symbol"] = "BTC/USDT"
        self.assertEqual(self.analyze()["decision"]["action"], "INSUFFICIENT_DATA")

    def test_resolution_failure_is_journaled_without_fetching_prices(self):
        def ambiguous(*args, **kwargs):
            return {"status": "ambiguous", "candidates": [{"symbol": "ABC/USDT"}, {"symbol": "XYZ/USDT"}],
                    "reasons": ["Choose an exact pair."], "warnings": []}
        report = self.make_service(resolver=ambiguous).analyze(AnalysisRequest("Example"))
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertEqual(report["resolution"]["status"], "ambiguous")
        self.assertIsNone(report["plan"])
        self.assertEqual(self.client.calls, [])
        self.assertTrue(report["journal"]["saved"])

    def test_wrong_resolved_or_scanned_market_is_rejected(self):
        def wrong_market(*args, **kwargs):
            wrong = {**self.market, "quote": "USDC"}
            return {"status": "resolved", "market": wrong, "warnings": []}
        report = self.make_service(resolver=wrong_market).analyze(AnalysisRequest("Quant"))
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.scan.symbol = "BTC/USDT"
        report = self.analyze()
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertEqual(report["charts"], [])

    def test_scanner_initialization_failure_is_a_journaled_data_failure(self):
        def unavailable(**kwargs):
            raise RuntimeError("private provider failure details")
        report = self.make_service(scanner_factory=unavailable).analyze(AnalysisRequest("Quant"))
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertTrue(report["journal"]["saved"])
        self.assertNotIn("private provider", json.dumps(report))

    def test_optional_fundamentals_preserve_technical_policy_and_expose_review_context(self):
        self.assertEqual(self.analyze(include_fundamentals=True)["decision"]["action"], "LONG_READY")
        for change in ("stale_market", "missing_events", "critical", "wrong_identity"):
            self.fundamentals = Fundamentals()
            if change == "stale_market":
                self.fundamentals.report["sources"][0]["as_of"] = (NOW - timedelta(days=2)).isoformat()
            elif change == "missing_events":
                self.fundamentals.report["sources"][1]["status"] = "unavailable"
            elif change == "critical":
                self.fundamentals.report["review_required"] = True
            else:
                self.fundamentals.report["identity"]["symbol"] = "BTC"
            with self.subTest(change=change):
                report = self.analyze(include_fundamentals=True)
                self.assertEqual(report["decision"]["action"], "LONG_READY")
                self.assertEqual(report["fundamentals"]["decision_role"], "optional_manual_context")
                self.assertEqual(report["decision"]["manual_review_required"], change == "critical")
                expected = {"stale_market": "project evidence", "missing_events": "announcement context",
                            "critical": "Critical event evidence", "wrong_identity": "project evidence"}[change]
                self.assertTrue(any(expected in warning for warning in report["warnings"]))

    def test_explicit_technical_only_mode_does_not_call_fundamental_providers(self):
        report = self.analyze()
        self.assertEqual(report["decision"]["action"], "LONG_READY")
        self.assertEqual(self.fundamentals.calls, 0)
        self.assertEqual(report["fundamentals"]["status"], "disabled")
        self.assertTrue(any("not requested" in warning for warning in report["warnings"]))
        self.assertEqual(self.scanner_options[0]["strategy_names"], ("trend_continuation_v2",))
        self.assertEqual(report["policy"]["settings"]["indicator_window"], 400)

    def test_corrupt_geometry_and_exit_allocations_are_rejected(self):
        service = self.make_service()
        request = AnalysisRequest("Quant")
        for changes in (
            {"entry_low": 101}, {"reference_entry": 110}, {"stop_loss": 100},
            {"take_profit_1": 99}, {"take_profit_2": 109}, {"take_profit_1_fraction": float("nan")},
            {"take_profit_1_fraction": True}, {"take_profit_1_fraction": 1.5},
            {"cancel_price": "not-a-price"}, {"status": "FORMING"},
        ):
            with self.subTest(changes=changes):
                plan, errors = service._plan({**proposed_plan(), **changes}, request, {}, self.market)
                self.assertIsNone(plan)
                self.assertTrue(errors)

    def test_corrupt_contract_multipliers_cannot_crash_or_qualify(self):
        self.use_swap()
        for multiplier in (None, 0, -1, True, float("nan"), float("inf"), "invalid"):
            self.market["contractSize"] = multiplier
            with self.subTest(multiplier=multiplier):
                report = self.analyze(market_type="swap")
                self.assertFalse(report["decision"]["action"].endswith("_READY"))
                self.assertIsNone(report["plan"])

    def test_short_notional_cap_covers_the_high_end_of_the_entry_zone(self):
        self.use_swap()
        self.scan.setup.update(entry_low=90, entry_high=110, stop_loss=120,
                               take_profit_1=60, take_profit_2=20, position_size=1000)
        report = self.analyze(market_type="swap", equity=100_000, risk_percent=5, notional=1000)
        self.assertEqual(report["decision"]["action"], "SHORT_READY")
        costs = report["plan"]["costs"]
        self.assertLessEqual(costs["maximum_entry_notional"] * (1 + costs["fee_rate_per_side"]), 1000 + 1e-10)
        self.assertLessEqual(report["plan"]["position_size"] * 110, 1000)

    def test_costs_and_exchange_minimums_can_block_an_otherwise_confirmed_setup(self):
        report = self.analyze(fee_rate=0.05, slippage_bps=500)
        self.assertEqual(report["decision"]["action"], "NO_TRADE")
        self.market["limits"] = {"amount": {"min": 10_000}}
        report = self.analyze()
        self.assertEqual(report["decision"]["action"], "NO_TRADE")
        self.assertTrue(any("minimum amount" in reason for reason in report["decision"]["blockers"]))

    def test_prices_outside_entry_or_past_invalidation_do_not_qualify(self):
        self.client.book.update(bids=[[100.95, 1000]], asks=[[101.05, 1000]])
        self.assertEqual(self.analyze()["decision"]["action"], "WAIT")
        self.client.book.update(bids=[[94.90, 1000]], asks=[[94.95, 1000]])
        self.assertEqual(self.analyze()["decision"]["action"], "NO_TRADE")

    def test_journal_failure_never_returns_an_unsaved_ready_report(self):
        class BrokenJournal:
            def save(self, report):
                raise RuntimeError("Journal storage unavailable")
        service = self.make_service(journal=BrokenJournal())
        with self.assertRaisesRegex(RuntimeError, "Journal storage"):
            service.analyze(AnalysisRequest("Quant"))

    def test_real_scanner_and_charts_exclude_newer_lower_timeframe_bars(self):
        newer = self.frames["15m"].tail(2).copy()
        newer["timestamp"] = [DECISION_CLOSE, DECISION_CLOSE + pd.Timedelta(minutes=15)]
        newer.loc[:, ["open", "high", "low", "close"]] = [500, 501, 499, 500]
        self.frames["15m"] = pd.concat([self.frames["15m"], newer], ignore_index=True)
        service = self.make_service(scanner_factory=MarketScanner)
        report = service.analyze(AnalysisRequest("Quant", include_fundamentals=False))
        self.assertNotEqual(report["decision"]["action"], "INSUFFICIENT_DATA", report["decision"]["blockers"])
        self.assertEqual({chart["timeframe"] for chart in report["charts"]}, {"4h", "1h", "15m"})
        for chart in report["charts"]:
            delta = pd.Timedelta(chart["timeframe"].replace("m", "min"))
            self.assertLessEqual(pd.Timestamp(chart["candles"][-1]["timestamp"]) + delta, DECISION_CLOSE)
            self.assertNotEqual(chart["candles"][-1]["close"], 500)

    def test_real_scanner_decision_frames_are_copies_of_the_frozen_cache(self):
        scanner = MarketScanner(exchange_id="kucoin", timeframe="1h", confirmation_timeframes=("4h",),
                                entry_timeframe="15m", now=lambda: pd.Timestamp(NOW),
                                data_fetcher=lambda timeframe, **kwargs: self.frames[timeframe].copy(deep=True))
        result = scanner.scan_symbol("QNT/USDT")
        self.assertIsNone(result.error)
        exported = scanner.decision_frames()
        original_close = exported["1h"].iloc[-1]["close"]
        exported["1h"].iloc[-1, exported["1h"].columns.get_loc("close")] = -999
        self.assertEqual(scanner.decision_frames()["1h"].iloc[-1]["close"], original_close)

    def test_missing_real_scanner_history_remains_insufficient_data(self):
        self.frames["4h"] = self.frames["4h"].tail(10).copy()
        report = self.make_service(scanner_factory=MarketScanner).analyze(AnalysisRequest("Quant"))
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertIsNone(report["plan"])

    def test_failed_primary_data_does_not_present_a_measured_zero_score(self):
        self.frames["1h"] = self.frames["1h"].tail(10).copy()
        report = self.make_service(scanner_factory=MarketScanner).analyze(AnalysisRequest("Quant"))
        self.assertEqual(report["decision"]["action"], "INSUFFICIENT_DATA")
        self.assertIsNone(report["decision"]["score"])

    def test_database_path_uses_working_directory_for_editable_and_wheel_installs(self):
        with patch("trading_bot.analysis.service.__file__", "/usr/local/lib/python3.12/site-packages/trading_bot/analysis/service.py"), \
             patch("trading_bot.analysis.service.Path.cwd", return_value=Path("/app")):
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(default_journal_path(), Path("/app/data/analysis/reports.db"))
            with patch.dict("os.environ", {"TRADING_ANALYSIS_DB": "custom/reports.db"}):
                self.assertEqual(default_journal_path(), Path("/app/custom/reports.db"))
            with patch.dict("os.environ", {"TRADING_ANALYSIS_DB": "/data/analysis/reports.db"}):
                self.assertEqual(default_journal_path(), Path("/data/analysis/reports.db"))


if __name__ == "__main__":
    unittest.main()
