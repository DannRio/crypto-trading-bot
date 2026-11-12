"""Controller and HTTP acceptance checks with real account ownership, no network."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading_bot.entry import main
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.locking import writer_active
from trading_bot.paper.runtime import BotSettings, PaperRuntime, RuntimeConflict
import trading_bot.web.app as web


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("The expected bounded lifecycle transition did not complete.")


class ControlledService:
    def __init__(self, config, journal):
        self.config, self.journal = config, journal
        self.thread_id = get_ident()
        self.broker = self

    def snapshot(self):
        return {"equity": self.config.starting_equity, "realized_net_pnl": 0}

    def run_forever(self, stop_event, on_cycle, on_error):
        count = int(self.journal.metadata().get("acceptance_cycles", "0")) + 1
        self.journal.set_metadata("acceptance_cycles", str(count))
        on_cycle({"snapshot": self.snapshot(), "scan_errors": 0, "management_errors": 0,
                  "risk_halted": False, "cycle": count})
        stop_event.wait(5)

    def close(self):
        self.journal.close()


class CoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "account" / "core_paper.db"
        self.instances = []

        def factory(**kwargs):
            service = ControlledService(**kwargs)
            self.instances.append(service)
            return service

        self.runtime = PaperRuntime(self.path, service_factory=factory)
        self.addCleanup(self.runtime.shutdown)

    def test_status_is_read_only_and_does_not_autostart_or_create_account(self):
        self.assertEqual(self.runtime.status()["state"], "stopped")
        self.assertFalse(self.runtime.status()["active"])
        self.assertFalse(self.path.parent.exists())

    def test_start_stop_restart_preserves_ledger_and_releases_exclusive_owner(self):
        self.runtime.start(BotSettings(symbols=("BTC/USDT",), notional=750))
        eventually(lambda: self.runtime.status()["state"] == "running")
        self.assertNotEqual(self.instances[0].thread_id, get_ident())
        self.assertTrue(writer_active(self.path))
        with self.assertRaises(RuntimeConflict):
            self.runtime.start(BotSettings())
        other = PaperRuntime(self.path, service_factory=ControlledService)
        self.assertEqual(other.status()["state"], "external")
        with self.assertRaises(RuntimeConflict):
            other.start(BotSettings())
        with self.assertRaises(RuntimeConflict):
            other.stop()
        self.runtime.stop()
        self.assertTrue(self.runtime.shutdown(3))
        self.assertFalse(writer_active(self.path))
        with PaperJournal.open_readonly(self.path) as reader:
            self.assertEqual(reader.metadata()["acceptance_cycles"], "1")
        resumed = PaperRuntime(self.path, service_factory=ControlledService)
        self.addCleanup(resumed.shutdown)
        self.assertEqual(resumed.status()["config"]["notional"], 750)
        self.assertFalse(resumed.status()["active"])
        resumed.start(BotSettings(**resumed.status()["config"]))
        eventually(lambda: resumed.status()["state"] == "running")
        resumed.stop()
        self.assertTrue(resumed.shutdown(3))
        with PaperJournal.open_readonly(self.path) as reader:
            self.assertEqual(reader.metadata()["acceptance_cycles"], "2")

    def test_ownership_uncertainty_blocks_both_lifecycle_mutations(self):
        runtime = PaperRuntime(self.path, owner_probe=lambda _: None)
        self.assertEqual(runtime.status()["health"], "ownership_unknown")
        with self.assertRaises(RuntimeConflict):
            runtime.start(BotSettings())
        with self.assertRaises(RuntimeConflict):
            runtime.stop()
        self.assertFalse(self.path.parent.exists())

    def test_failed_start_keeps_saved_settings_and_exposes_safe_error(self):
        self.path.parent.mkdir()
        original = {"schema_version": 1, "settings": {"notional": 500}}
        self.runtime.settings_path.write_text(json.dumps(original))

        def fail(**kwargs):
            raise ValueError("configuration does not match; private provider details")

        runtime = PaperRuntime(self.path, service_factory=fail)
        self.addCleanup(runtime.shutdown)
        runtime.start(BotSettings(notional=750))
        eventually(lambda: runtime.status()["state"] == "error" and not runtime.status()["active"])
        self.assertIn("saved account", runtime.status()["message"])
        self.assertNotIn("private provider", json.dumps(runtime.status()))
        self.assertEqual(json.loads(runtime.settings_path.read_text()), original)
        self.assertEqual(runtime.status()["config"]["notional"], 500)
        self.assertFalse(writer_active(self.path))

    def test_stale_cycle_is_not_reported_healthy(self):
        clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        self.runtime.now = lambda: clock[0]
        self.runtime.start(BotSettings())
        eventually(lambda: self.runtime.status()["state"] == "running")
        clock[0] += timedelta(minutes=16)
        self.assertEqual(self.runtime.status()["health"], "stale")
        self.assertTrue(self.runtime.status()["active"])

    def test_invalid_saved_configuration_cannot_look_like_a_started_account(self):
        self.path.parent.mkdir()
        self.runtime.settings_path.write_text('{"schema_version": 99}')
        runtime = PaperRuntime(self.path)
        self.assertEqual(runtime.status()["health"], "configuration_error")
        self.assertFalse(runtime.status()["active"])
        self.assertFalse(self.path.exists())

    def test_strict_watchlist_quote_and_unique_symbols(self):
        self.assertEqual(BotSettings(symbols=[" btc/usdt "]).symbols, ("BTC/USDT",))
        for values in ({"symbols": ["BTC/USD"]}, {"symbols": ["BTC/USDT:USDT"]},
                       {"symbols": ["BTC/USDT", "btc/usdt"]}, {"notional": float("nan")},
                       {"risk_percent": True}, {"max_markets": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BotSettings(**values)

    def test_http_lifecycle_uses_same_account_and_rejects_cross_origin_commands(self):
        with patch.object(web, "_bot_runtime", return_value=self.runtime), \
             patch.object(web, "PAPER_JOURNAL_PATH", self.path), TestClient(web.app) as client:
            response = client.get("/api/bot/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["paper"]["status"], "not_started")
            self.assertFalse(self.path.exists())
            for endpoint in ("/api/bot/start", "/api/bot/stop", "/api/history/prepare"):
                body = {} if endpoint != "/api/history/prepare" else {
                    "exchange": "kucoin", "symbol": "BTC/USDT", "start": "2025-01-01", "end": "2025-01-02"}
                self.assertEqual(client.post(endpoint, json=body, headers={"origin": "https://unrelated.invalid"}).status_code, 403)
            self.assertEqual(client.post("/api/bot/start", json={"symbols": ["BTC/USD"]}).status_code, 422)
            response = client.post("/api/bot/start", json={"symbols": ["BTC/USDT"]})
            self.assertEqual(response.status_code, 202)
            eventually(lambda: self.runtime.status()["state"] == "running")
            self.assertEqual(client.post("/api/bot/start", json={}).status_code, 409)
            report = client.get("/api/bot/status").json()
            self.assertEqual(report["paper"]["account"]["equity"], 10000)
            self.assertTrue(client.get("/api/health").json()["paper_execution"])
            self.assertFalse(report["capabilities"]["live_orders"])
            self.assertEqual(client.post("/api/bot/stop").status_code, 202)
        self.assertTrue(self.runtime.shutdown(3))

    def test_corrupt_account_read_returns_error_without_reset(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not a sqlite account")
        with patch.object(web, "PAPER_JOURNAL_PATH", self.path), TestClient(web.app) as client:
            response = client.get("/api/paper/status")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.path.read_bytes(), b"not a sqlite account")

    def test_launcher_uses_one_worker_and_data_directory_without_starting_it(self):
        with patch.dict("os.environ", {}, clear=False), patch("uvicorn.run") as run:
            self.assertEqual(main(["--data-dir", str(Path(self.temp.name) / "separate"), "--port", "8087"]), 0)
            run.assert_called_once_with("trading_bot.web.app:app", host="127.0.0.1", port=8087, workers=1)
            import os
            self.assertEqual(Path(os.environ["TRADING_PAPER_JOURNAL"]).parts[-3:], ("separate", "paper", "core_paper.db"))
        self.assertFalse((Path(self.temp.name) / "separate").exists())


if __name__ == "__main__":
    unittest.main()
