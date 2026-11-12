"""Account recovery acceptance tests using deterministic local market data."""

from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import pandas as pd

from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.paper.broker import PaperBroker
from trading_bot.paper.journal import PaperJournal, PaperJournalIntegrityError, PaperPersistenceError
from trading_bot.paper.locking import PaperAccountInUseError, writer_active
from trading_bot.paper.models import PaperOrderStatus
from trading_bot.paper.service import PaperServiceConfig, PaperTradingService
from trading_bot.planning import TradeSetup


SYMBOL = "BTC/USDT"
KEY = (SYMBOL, "1h")
START = pd.Timestamp("2026-01-01T00:00:00Z")


def setup():
    return TradeSetup(
        signal=Signal.LONG, status=SetupStatus.CONFIRMED, score=80,
        reference_entry=100, entry_low=99, entry_high=101,
        stop_loss=95, take_profit_1=105, take_profit_2=110,
        reward_to_risk_1=1, reward_to_risk_2=2,
        position_size=2, notional_value=200,
        target_risk_amount=10, actual_risk_amount=10, reasons=("fixture",),
    )


def candle(hour, *, open_=100, high=101, low=99, close=100):
    return {"timestamp": START + pd.Timedelta(hours=hour),
            "open": open_, "high": high, "low": low, "close": close, "volume": 1000}


class EmptyScanner:
    def __init__(self, **kwargs):
        pass

    def scan(self, symbols):
        return []


class LongScanner(EmptyScanner):
    def scan(self, symbols):
        from types import SimpleNamespace
        return [SimpleNamespace(symbol=symbol, timeframe="1h", error=None, setup=setup(),
                                assessment=SimpleNamespace(status=SetupStatus.CONFIRMED,
                                    signal=Signal.LONG, strategy="trend_continuation"),
                                structure=SimpleNamespace(regime=MarketRegime.BULL_TREND))
                for symbol in symbols]


class PaperRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "account.db"
        self.journals = []

    def tearDown(self):
        for journal in self.journals:
            journal.close()
        self.temporary.cleanup()

    def journal(self):
        journal = PaperJournal(self.path)
        self.journals.append(journal)
        return journal

    def broker(self, journal, fee_rate=0):
        return PaperBroker(starting_equity=10_000, fee_rate=fee_rate,
                           slippage_bps=0, journal=journal)

    def filled(self, broker):
        order = broker.submit_setup(*KEY, setup(), START)
        broker.process_candle(*KEY, candle(1))
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        return order

    def service(self, journal, rows=None, now=None, **kwargs):
        rows = rows if rows is not None else [candle(hour) for hour in range(4)]
        config = kwargs.pop("config", PaperServiceConfig(
            exchange_id="fixture", symbols=(SYMBOL,), fee_rate=0, slippage_bps=0,
            liquidity_preflight=False, interval_seconds=3600))
        return PaperTradingService(config=config, journal=journal,
            data_fetcher=lambda **_: pd.DataFrame(rows),
            utc_now=lambda: now if now is not None else START + pd.Timedelta(hours=3, minutes=30),
            scanner_factory=kwargs.pop("scanner_factory", EmptyScanner), **kwargs)

    def test_close_failure_rolls_back_order_trade_and_memory_then_retries_once(self):
        journal = self.journal()
        broker = self.broker(journal)
        order = self.filled(broker)
        before = broker.snapshot()
        original = journal.save_trade

        def fail_after_trade_insert(trade):
            original(trade)
            raise OSError("injected trade persistence failure")

        with patch.object(journal, "save_trade", fail_after_trade_insert):
            with self.assertRaisesRegex(OSError, "injected"):
                broker.process_candle(*KEY, candle(2, open_=96, high=96, low=94, close=94))
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(broker.snapshot(), before)
        self.assertEqual(journal.active_orders().iloc[0]["status"], "FILLED")
        self.assertTrue(journal.all_trades().empty)
        journal.close()

        reopened = self.journal()
        recovered = self.broker(reopened)
        recovered.restore_from_journal()
        trades = recovered.process_candle(*KEY, candle(2, open_=96, high=96, low=94, close=94))
        self.assertEqual(len(trades), 1)
        self.assertEqual(recovered.realized_net_pnl, -10)
        self.assertEqual(len(reopened.all_trades()), 1)
        recovered.process_candle(*KEY, candle(2, open_=96, high=96, low=94, close=94))
        self.assertEqual(recovered.realized_net_pnl, -10)
        self.assertEqual(len(reopened.all_trades()), 1)
        reopened.close()
        final = self.broker(self.journal())
        final.restore_from_journal()
        self.assertEqual(final.realized_net_pnl, -10)
        self.assertEqual(final.account_equity(), 9990)

    def test_checkpoint_failure_cannot_commit_a_close_or_advance_risk(self):
        journal = self.journal()
        service = self.service(journal, rows=[candle(1),
            candle(2, open_=96, high=96, low=94, close=94), candle(3)])
        self.filled(service.broker)
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=1))
        before = service.broker.snapshot()
        original = journal.set_checkpoint

        def fail_after_checkpoint(*args):
            original(*args)
            raise OSError("injected checkpoint failure")

        with patch.object(journal, "set_checkpoint", fail_after_checkpoint):
            with self.assertRaisesRegex(OSError, "checkpoint"):
                service._process_unseen_candles(SYMBOL)
        self.assertEqual(service.broker.snapshot(), before)
        self.assertIsNone(journal.load_risk_state())
        self.assertEqual(journal.get_checkpoint(*KEY), START + pd.Timedelta(hours=1))
        self.assertTrue(journal.all_trades().empty)
        journal.close()

        restarted = self.service(self.journal(), rows=[candle(1),
            candle(2, open_=96, high=96, low=94, close=94), candle(3)])
        self.assertEqual(restarted._process_unseen_candles(SYMBOL)[:2], (1, 1))
        self.assertEqual(restarted._process_unseen_candles(SYMBOL)[:2], (0, 0))
        self.assertEqual(len(restarted.journal.all_trades()), 1)
        self.assertEqual(restarted.broker.realized_net_pnl, -10)
        self.assertIsNotNone(restarted.journal.load_risk_state())

    def test_checkpoint_failure_does_not_consume_pending_expiry_twice(self):
        journal = self.journal()
        service = self.service(journal, rows=[candle(hour, open_=103, high=104, low=102, close=103)
                                            for hour in range(4)])
        order = service.broker.submit_setup(*KEY, setup(), START)
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=1))
        with patch.object(journal, "set_checkpoint", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                service._process_unseen_candles(SYMBOL)
        self.assertEqual(order.bars_remaining, 3)
        self.assertEqual(journal.active_orders().iloc[0]["bars_remaining"], 3)
        service._process_unseen_candles(SYMBOL)
        self.assertEqual(order.bars_remaining, 2)
        service._process_unseen_candles(SYMBOL)
        self.assertEqual(order.bars_remaining, 2)

    def test_uncertain_commit_stops_writer_and_restart_observes_one_committed_step(self):
        journal = self.journal()
        service = self.service(journal, rows=[candle(1),
            candle(2, open_=96, high=96, low=94, close=94), candle(3)])
        self.filled(service.broker)
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=1))

        class AcknowledgementLost:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.connection.commit()
                raise OSError("commit acknowledgement lost after durable commit")

        journal.connection = AcknowledgementLost(journal.connection)
        with self.assertRaises(PaperPersistenceError):
            service._process_unseen_candles(SYMBOL)
        self.assertTrue(journal.writer_failed)
        with self.assertRaises(PaperPersistenceError):
            service.run_once()
        journal.close()
        restarted = self.service(self.journal(), rows=[candle(1),
            candle(2, open_=96, high=96, low=94, close=94), candle(3)])
        self.assertEqual(restarted._process_unseen_candles(SYMBOL)[:2], (0, 0))
        self.assertEqual(len(restarted.journal.all_trades()), 1)
        self.assertEqual(restarted.broker.realized_net_pnl, -10)

    def test_single_writer_rejects_same_and_other_process_readers_still_work(self):
        self.assertFalse(writer_active(self.path))
        self.assertFalse(self.path.exists())
        journal = self.journal()
        journal.set_metadata("test", "saved")
        self.assertTrue(writer_active(self.path))
        with self.assertRaises(PaperAccountInUseError):
            PaperJournal(self.path)
        with PaperJournal.open_readonly(self.path) as reader:
            self.assertEqual(reader.metadata()["test"], "saved")
            with self.assertRaises(RuntimeError):
                reader.set_metadata("test", "changed")
        lock_module = Path(__file__).resolve().parents[1] / "src/trading_bot/paper/locking.py"
        script = """import importlib.util, sys
spec = importlib.util.spec_from_file_location('lock_test', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
try:
    lock = mod.JournalWriterLock(sys.argv[2]).acquire()
except mod.PaperAccountInUseError:
    print('blocked')
else:
    lock.close()
    print('acquired')
"""
        result = subprocess.run([sys.executable, "-c", script, str(lock_module), str(self.path)],
                                text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "blocked")
        journal.close()
        self.assertFalse(writer_active(self.path))
        result = subprocess.run([sys.executable, "-c", script, str(lock_module), str(self.path)],
                                text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "acquired")
        self.journal().set_metadata("test", "resumed")

    def test_readonly_missing_account_creates_nothing(self):
        with self.assertRaises(FileNotFoundError):
            PaperJournal.open_readonly(self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".writer.lock").exists())

    def test_gap_from_checkpoint_halts_without_repricing_or_advancing(self):
        journal = self.journal()
        service = self.service(journal, rows=[candle(100), candle(101), candle(102)],
                               now=START + pd.Timedelta(hours=102, minutes=30))
        self.filled(service.broker)
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=1))
        before = service.broker.snapshot()
        with self.assertRaisesRegex(ValueError, "missing recovery history"):
            service._process_unseen_candles(SYMBOL)
        self.assertEqual(service.broker.snapshot(), before)
        self.assertEqual(journal.get_checkpoint(*KEY), START + pd.Timedelta(hours=1))
        report = service.run_once()
        self.assertEqual(report.management_errors, 1)
        self.assertIn("missing recovery history", report.management_error_details[0])
        self.assertEqual(report.submitted_orders, 0)

    def test_missing_or_future_checkpoint_never_skips_existing_exposure(self):
        journal = self.journal()
        service = self.service(journal)
        self.filled(service.broker)
        with self.assertRaisesRegex(ValueError, "no candle checkpoint"):
            service._process_unseen_candles(SYMBOL)
        self.assertIsNone(journal.get_checkpoint(*KEY))
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=10))
        with self.assertRaisesRegex(ValueError, "ahead"):
            service._process_unseen_candles(SYMBOL)
        self.assertEqual(journal.get_checkpoint(*KEY), START + pd.Timedelta(hours=10))

    def test_pending_order_cannot_fill_or_expire_using_pre_submission_prices(self):
        journal = self.journal()
        broker = self.broker(journal)
        order = broker.submit_setup(*KEY, setup(), START + pd.Timedelta(hours=12, minutes=35))
        broker.process_candle(*KEY, candle(12))
        self.assertEqual(order.status, PaperOrderStatus.PENDING)
        self.assertEqual(order.bars_remaining, 3)
        broker.process_candle(*KEY, candle(13))
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.filled_at, START + pd.Timedelta(hours=13))

    def test_submit_and_signal_dedup_are_atomic_and_use_actual_submission_clock(self):
        journal = self.journal()
        service = self.service(journal, scanner_factory=LongScanner)
        original = journal.record_signal

        def fail_after_signal(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("signal write failed")

        with patch.object(journal, "record_signal", fail_after_signal):
            with self.assertRaises(OSError):
                service.run_once()
        self.assertTrue(journal.active_orders().empty)
        self.assertTrue(journal.signal_events().empty)
        self.assertFalse(service.broker.has_exposure(*KEY))
        self.assertEqual(service.run_once().submitted_orders, 1)
        order = service.broker.pending_orders[0]
        self.assertEqual(order.created_at, START + pd.Timedelta(hours=3, minutes=30))
        self.assertEqual(service.run_once().duplicate_signals, 1)
        self.assertEqual(len(journal.active_orders()), 1)

    def test_preflight_expiry_and_resized_setup_are_applied_and_journaled(self):
        journal = self.journal()
        config = PaperServiceConfig(exchange_id="fixture", symbols=(SYMBOL,), fee_rate=0,
            slippage_bps=0, liquidity_preflight=True, policy_fingerprint="policy-fixture")
        checked = START + pd.Timedelta(hours=3, minutes=30)
        resized = replace(setup(), position_size=1, notional_value=100, actual_risk_amount=5)
        preflight = {"allowed": True, "setup": resized, "reasons": [],
                     "checked_at": checked.isoformat(), "expires_at": (checked + pd.Timedelta(minutes=2)).isoformat(),
                     "snapshots": {"ticker": {"last": 100}}}
        service = self.service(journal, config=config, scanner_factory=LongScanner,
                               entry_preflight=lambda **_: preflight)
        self.assertEqual(service.run_once().submitted_orders, 1)
        self.assertEqual(service.broker.pending_orders[0].position_size, 1)
        evidence = json.loads(journal.signal_events().iloc[0]["evidence"])
        self.assertEqual(evidence["policy_fingerprint"], "policy-fixture")
        self.assertEqual(evidence["snapshots"]["ticker"]["last"], 100)
        service.close()

        self.path = Path(self.temporary.name) / "expired.db"
        preflight["expires_at"] = checked.isoformat()
        expired = self.service(self.journal(), config=config, scanner_factory=LongScanner,
                               entry_preflight=lambda **_: preflight)
        report = expired.run_once()
        self.assertEqual(report.submitted_orders, 0)
        self.assertEqual(report.rejected_signals, 1)
        self.assertIn("expired", expired.journal.signal_events().iloc[0]["reason"])

    def test_stop_event_and_close_release_account_without_closing_positions(self):
        journal = self.journal()
        service = self.service(journal)
        self.filled(service.broker)
        journal.set_checkpoint(*KEY, START + pd.Timedelta(hours=2))
        stop = Event()
        reports = []

        def on_cycle(report):
            reports.append(report)
            stop.set()

        service.run_forever(stop_event=stop, on_cycle=on_cycle)
        self.assertEqual(len(reports), 1)
        self.assertTrue(writer_active(self.path))
        service.close()
        service.close()
        self.assertFalse(writer_active(self.path))
        with PaperJournal.open_readonly(self.path) as reader:
            self.assertEqual(json.loads(reader.metadata()["heartbeat"])["status"], "stopped")
            self.assertEqual(reader.active_orders().iloc[0]["status"], "FILLED")

    def test_damaged_legacy_close_is_rejected_without_resetting_account(self):
        journal = self.journal()
        broker = self.broker(journal)
        order = self.filled(broker)
        journal.connection.execute("UPDATE paper_orders SET status='CLOSED' WHERE order_id=?", (order.order_id,))
        journal.connection.commit()
        journal.close()
        with self.assertRaises(PaperJournalIntegrityError):
            PaperJournal(self.path)
        self.assertFalse(writer_active(self.path))
        with PaperJournal.open_readonly(self.path) as reader:
            self.assertEqual(reader.orders().iloc[0]["status"], "CLOSED")
            self.assertTrue(reader.all_trades().empty)

    def test_fill_and_restart_reserve_costed_risk(self):
        journal = self.journal()
        broker = self.broker(journal, fee_rate=.001)
        self.filled(broker)
        position = broker.positions[KEY]
        expected = (100 - 95 + .001 * (100 + 95)) * position.position_size
        self.assertAlmostEqual(position.risk_amount, expected)
        self.assertAlmostEqual(position.initial_risk_amount, expected)
        restored = self.broker(journal, fee_rate=.001)
        restored.restore_from_journal()
        self.assertAlmostEqual(restored.positions[KEY].risk_amount, expected)
        self.assertAlmostEqual(restored.realized_net_pnl, broker.realized_net_pnl)

    def test_default_cli_can_resume_equivalent_dashboard_policy(self):
        from trading_bot.paper import cli
        from trading_bot.paper.runtime import BotSettings, build_service_config
        journal = self.journal()
        dashboard_config = build_service_config(BotSettings(symbols=(SYMBOL,)))
        original = self.service(journal, config=dashboard_config)
        original.close()

        def create_service(*, config, journal):
            return self.service(journal, config=config)

        with patch.object(cli, "PaperTradingService", create_service), redirect_stdout(StringIO()):
            result = cli.main(["daemon", "--symbols", SYMBOL, "--journal", str(self.path), "--once"])
        self.assertEqual(result, 0)
        self.assertFalse(writer_active(self.path))

    def test_portfolio_recovery_does_not_rewind_risk_periods(self):
        broker = self.broker(self.journal())
        later = START + pd.Timedelta(days=2)
        broker.evaluate_account_risk(later)
        broker.realized_net_pnl = -100
        broker.evaluate_account_risk(START)
        self.assertEqual(broker.risk_state.evaluated_at, later)
        self.assertEqual(broker.risk_state.day_start_equity, 10_000)

    def test_persistence_error_stops_forever_loop_even_with_error_callback(self):
        service = self.service(self.journal())
        errors = []
        with patch.object(service, "run_once", side_effect=PaperPersistenceError("unverified commit")) as run:
            with self.assertRaises(PaperPersistenceError):
                service.run_forever(on_error=errors.append, max_cycles=2, sleep_fn=lambda _: None)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
