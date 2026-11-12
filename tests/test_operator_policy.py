"""Operator-policy boundaries: causal data, absolute budgets and public evidence."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from trading_bot.analysis import add_indicators, decision_indicators
from trading_bot.analysis.preflight import public_entry_preflight
from trading_bot.backtesting import run_decision_backtest
from trading_bot.backtesting.timeframe_gates import HistoricalTimeframeGates
from trading_bot.config import INDICATOR_WINDOW, operator_policy
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.data import HistoricalDataStore
from trading_bot.exchanges import ccxt_client
from trading_bot.planning import TradeSetup, revalidate_trade_plan
from trading_bot.scanner import DynamicMarketUniverse, MarketScanner, UniverseConfig


NOW = datetime(2026, 11, 12, 12, 47, tzinfo=timezone.utc)
CLOSE = pd.Timestamp("2026-11-12T12:00:00Z")


def candles(timeframe, rows=1100):
    delta = pd.Timedelta(timeframe.replace("m", "min"))
    end = pd.Timestamp(NOW).floor(delta)
    # An old price regime makes full-history EMA initialization visibly differ.
    prices = [1000.0] * max(0, rows - 500) + [100 + .01 * i for i in range(min(rows, 500))]
    return pd.DataFrame({
        "timestamp": pd.date_range(end=end, periods=rows, freq=delta),
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [1000.0] * rows,
    })


def trade_setup():
    return TradeSetup(
        signal=Signal.LONG, status=SetupStatus.CONFIRMED, score=80,
        reference_entry=100, entry_low=99.5, entry_high=100.5, stop_loss=95,
        take_profit_1=110, take_profit_2=120, reward_to_risk_1=2, reward_to_risk_2=4,
        position_size=1000, notional_value=100000, target_risk_amount=25,
        actual_risk_amount=5000, reasons=("Synthetic risk-boundary test.",),
    )


class PublicClient:
    def __init__(self):
        self.market = {"symbol": "QNT/USDT", "base": "QNT", "quote": "USDT",
                       "type": "spot", "spot": True, "active": True, "limits": {}}
        self.ticker = {"symbol": "QNT/USDT", "timestamp": int(NOW.timestamp() * 1000),
                       "bid": 99.95, "ask": 100.05, "last": 100, "quoteVolume": 5_000_000}
        self.book = {"symbol": "QNT/USDT", "timestamp": self.ticker["timestamp"],
                     "bids": [[99.95, 1000]], "asks": [[100.05, 1000]]}
        self.reloads = []

    def load_markets(self, reload=False):
        self.reloads.append(reload)
        return {self.market["symbol"]: self.market}

    def fetch_ticker(self, symbol):
        return deepcopy(self.ticker)

    def fetch_order_book(self, symbol):
        return deepcopy(self.book)


class OperatorPolicyTests(unittest.TestCase):
    def test_policy_hash_and_absolute_cap_survive_account_equity_changes(self):
        first = operator_policy(equity=10000, notional=1000)
        self.assertEqual(first["fingerprint"], operator_policy(equity=10100, notional=1000)["fingerprint"])
        self.assertEqual(first["settings"]["risk_percent"], .0025)
        self.assertEqual(first["settings"]["strategy_names"], ("trend_continuation_v2",))
        self.assertEqual(operator_policy(equity=500, notional=1000)["settings"]["notional_cap"], 1000)
        for equity in (500, 10000, 10100):
            plan, errors = revalidate_trade_plan(trade_setup(), account_equity=equity,
                risk_percent=.05, notional_cap=1000, fee_rate=.001, slippage_bps=5)
            self.assertFalse(errors)
            self.assertLessEqual(plan["costs"]["maximum_entry_notional"] * 1.001, min(1000, equity) + 1e-9)
            self.assertLessEqual(plan["actual_risk_amount"], equity * .05 + 1e-9)

    def test_live_and_historical_secondary_indicators_share_exact_closed_window(self):
        full = {frame: candles(frame) for frame in ("1h", "4h", "15m")}
        scanner = MarketScanner(now=lambda: pd.Timestamp(NOW),
            data_fetcher=lambda timeframe, **kwargs: full[timeframe].tail(500).copy())
        scanner._decision_close = CLOSE
        gates = HistoricalTimeframeGates("1h", full, confirmation_timeframes=("4h",), entry_timeframe="15m")
        for timeframe in ("4h", "15m"):
            with self.subTest(timeframe=timeframe):
                live, _, error = scanner._fetch_closed("QNT/USDT", timeframe)
                historical, _ = gates._closed_snapshot(timeframe, CLOSE)
                self.assertIsNone(error)
                self.assertEqual(len(live), INDICATOR_WINDOW)
                pd.testing.assert_frame_equal(decision_indicators(live), historical)
                self.assertLessEqual(live.iloc[-1]["timestamp"] + pd.Timedelta(timeframe.replace("m", "min")), CLOSE)
        closed = full["1h"].loc[full["1h"]["timestamp"] < CLOSE]
        self.assertGreater(abs(add_indicators(closed).iloc[-1]["ema_200"]
                               - decision_indicators(closed).iloc[-1]["ema_200"]), 1.0)

    def test_scanner_requires_full_closed_window_even_if_response_contains_open_bar(self):
        raw = candles("1h", rows=400)
        scanner = MarketScanner(now=lambda: pd.Timestamp(NOW), data_fetcher=lambda **kwargs: raw)
        result = scanner.scan_symbol("QNT/USDT")
        self.assertIn("400 closed candles", result.error)
        raw = candles("1h", rows=401)
        frame, _, error = scanner._fetch_closed("QNT/USDT", "1h")
        self.assertIsNone(error)
        self.assertEqual(len(frame), 400)

    def test_backtest_recomputes_indicators_from_bounded_causal_primary_history(self):
        raw = candles("1h")
        observed = []
        empty = SimpleNamespace(assessment=SimpleNamespace(status=SetupStatus.NO_TRADE,
            strategy="trend_continuation_v2", signal=Signal.NO_TRADE, event_id=None), setup=None)
        class Engine:
            def __init__(self, **kwargs):
                pass
            def evaluate(self, history):
                observed.append(history.copy())
                return empty
        with patch("trading_bot.backtesting.decision_engine.DecisionEngine", Engine):
            run_decision_backtest(raw, warmup_candles=len(raw) - 3)
        self.assertEqual(len(observed), 2)
        for index, history in enumerate(observed, start=len(raw) - 3):
            pd.testing.assert_frame_equal(history, decision_indicators(raw.iloc[index - 399:index + 1]))

    def test_kucoin_swaps_use_one_futures_client_and_distinct_history_namespace(self):
        class Adapter:
            def __init__(self, config):
                self.config = config
        with patch.object(ccxt_client.ccxt, "kucoinfutures", Adapter, create=True), \
             patch.dict(ccxt_client._PUBLIC_CLIENTS, {}, clear=True):
            client = ccxt_client.get_public_client("kucoin", "swap")
            self.assertEqual(client.exchange_id, "kucoinfutures")
            self.assertEqual(client.exchange.config["timeout"], 10000)
            self.assertIs(client, ccxt_client.get_public_client("kucoinfutures", "swap"))
        with TemporaryDirectory() as directory:
            store = HistoricalDataStore(directory)
            swap = store.dataset_path("kucoin", "swap", "QNT/USDT:USDT", "1h")
            self.assertEqual(swap, store.dataset_path("kucoinfutures", "swap", "QNT/USDT:USDT", "1h"))
            self.assertIn("kucoinfutures", swap.parts)
            self.assertNotEqual(swap, store.dataset_path("kucoin", "spot", "QNT/USDT", "1h"))

    def test_failed_history_write_preserves_previous_complete_dataset(self):
        with TemporaryDirectory() as directory:
            store = HistoricalDataStore(directory)
            raw = candles("1h", rows=10)
            path = store.upsert("kucoin", "spot", "QNT/USDT", "1h", raw)
            previous = path.read_bytes()
            def interrupted(frame, handle, **kwargs):
                handle.write("incomplete data")
                raise OSError("Interrupted write")
            with patch.object(pd.DataFrame, "to_csv", interrupted):
                with self.assertRaisesRegex(OSError, "Interrupted"):
                    store.upsert("kucoin", "spot", "QNT/USDT", "1h", raw)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            pd.testing.assert_frame_equal(store.load("kucoin", "spot", "QNT/USDT", "1h"), raw)


class EntryPreflightTests(unittest.TestCase):
    def setUp(self):
        self.client = PublicClient()

    def preflight(self, **changes):
        options = dict(exchange_id="kucoin", market_type="spot", symbol="QNT/USDT",
                       setup=trade_setup(), account_equity=10000, risk_percent=.0025,
                       notional_cap=1000, fee_rate=.001, slippage_bps=5,
                       now=lambda: NOW, client=self.client)
        options.update(changes)
        return public_entry_preflight(**options)

    def test_valid_entry_returns_cost_capped_persistable_setup_and_public_evidence(self):
        result = self.preflight()
        self.assertTrue(result["allowed"], result["reasons"])
        self.assertIsInstance(result["setup"], TradeSetup)
        self.assertLessEqual(result["setup"].actual_risk_amount, 25 + 1e-9)
        self.assertEqual(result["plan"]["position_size"], result["setup"].position_size)
        self.assertEqual(self.client.reloads, [True])
        self.assertFalse(result["historical_backtest_eligible"])
        self.assertEqual(datetime.fromisoformat(result["expires_at"]), NOW + timedelta(seconds=120))

    def test_manual_symbol_cannot_bypass_volume_or_current_entry_price(self):
        self.client.ticker["quoteVolume"] = 100
        result = self.preflight()
        self.assertFalse(result["allowed"])
        self.assertTrue(any("volume" in item for item in result["reasons"]))
        self.client.ticker["quoteVolume"] = 5_000_000
        self.client.book.update(bids=[[101, 1000]], asks=[[101.1, 1000]])
        result = self.preflight()
        self.assertFalse(result["allowed"])
        self.assertTrue(any("outside the planned entry" in item for item in result["reasons"]))

    def test_stale_quotes_and_expiry_during_preflight_fail_closed(self):
        self.client.book["timestamp"] -= 121000
        self.assertFalse(self.preflight()["allowed"])
        self.client.book["timestamp"] = self.client.ticker["timestamp"]
        clock = iter([NOW, NOW, NOW, NOW + timedelta(seconds=121)])
        result = self.preflight(now=lambda: next(clock))
        self.assertFalse(result["allowed"])
        self.assertIn("Market evidence expired during entry preflight.", result["reasons"])

    def test_incomplete_setup_never_becomes_an_allowed_entry(self):
        incomplete = asdict(trade_setup())
        del incomplete["score"]
        result = self.preflight(setup=incomplete)
        self.assertFalse(result["allowed"])
        self.assertIsNone(result["setup"])


class UniverseCancellationTests(unittest.TestCase):
    def test_scanner_stops_between_timeframe_requests_and_discards_partial_results(self):
        stop, calls = Event(), []
        def fetch(timeframe, **kwargs):
            calls.append(timeframe)
            stop.set()
            return candles(timeframe).tail(500).copy()
        scanner = MarketScanner(confirmation_timeframes=("4h",), entry_timeframe="15m",
                                data_fetcher=fetch, now=lambda: pd.Timestamp(NOW))
        self.assertEqual(scanner.scan(["QNT/USDT", "BTC/USDT"], cancel=stop), [])
        self.assertEqual(calls, ["1h"])

    def test_cancel_between_history_probes_discards_partial_universe(self):
        stop = Event()
        calls = []
        class Client:
            def load_markets(self, reload=False):
                return {symbol: {"symbol": symbol, "base": symbol.split("/")[0], "quote": "USDT",
                                 "type": "spot", "active": True} for symbol in ("BTC/USDT", "ETH/USDT")}
            def fetch_tickers(self, symbols):
                return {symbol: {"quoteVolume": 5_000_000, "bid": 100, "ask": 100.01} for symbol in symbols}
            def fetch_ohlcv(self, symbol, timeframe, limit):
                calls.append(symbol)
                stop.set()
                raw = candles("1h", rows=3)
                raw["timestamp"] = raw["timestamp"].astype("int64") // 1_000_000
                return raw.values.tolist()
        selector = DynamicMarketUniverse("kucoin", "spot", client=Client(), now=lambda: NOW)
        result = selector.select(UniverseConfig(max_markets=2, min_history_candles=2), cancel=stop)
        self.assertEqual(calls, ["BTC/USDT"])
        self.assertEqual(result.symbols, [])
        self.assertIn("Universe selection cancelled.", result.warnings)

    def test_already_cancelled_selection_makes_no_public_calls(self):
        class Client:
            def load_markets(self, reload=False):
                raise AssertionError("Cancellation should prevent this request.")
        selector = DynamicMarketUniverse("kucoin", "spot", client=Client())
        self.assertFalse(selector.select(cancel=lambda: True).symbols)


if __name__ == "__main__":
    unittest.main()
