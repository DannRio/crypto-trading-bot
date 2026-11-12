"""Background historical preparation must never present partial data as ready."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from trading_bot.data.history import timeframe_to_milliseconds
from trading_bot.research.jobs import HistoryJobs, HistoryRequest, HistoryJobConflict
import trading_bot.web.app as web


def complete_history(**kwargs):
    delta = pd.to_timedelta(timeframe_to_milliseconds(kwargs["timeframe"]), unit="ms")
    stamps = pd.date_range(kwargs["start"], kwargs["end"], freq=delta, inclusive="left")
    return pd.DataFrame({"timestamp": stamps, "open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.})


class HistoryJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "history"
        self.clock = lambda: datetime(2026, 1, 10, 10, 30, tzinfo=timezone.utc)
        self.calls = []

        def fetch(**kwargs):
            self.calls.append(kwargs)
            return complete_history(**kwargs)

        self.jobs = HistoryJobs(self.root, client_factory=lambda *_: object(), fetcher=fetch, now=self.clock)
        self.addCleanup(self.jobs.shutdown)
        self.request = HistoryRequest("kucoin", "spot", "BTC/USDT", "2026-01-09", "2026-01-10")

    def finish(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.jobs.get(job["job_id"])
            if result["state"] != "running":
                return result
            time.sleep(.01)
        self.fail("History job did not finish in bounded synthetic test.")

    def test_prepares_all_three_complete_datasets_with_independent_warmup(self):
        result = self.finish(self.jobs.start(self.request))
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(result["end_clamped"])
        self.assertEqual(result["effective_end"], "2026-01-10T08:00:00+00:00")
        self.assertEqual([call["timeframe"] for call in self.calls], ["4h", "1h", "15m"])
        for call in self.calls:
            delta = pd.to_timedelta(timeframe_to_milliseconds(call["timeframe"]), unit="ms")
            self.assertEqual(call["start"], pd.Timestamp(self.request.start, tz="UTC") - 400 * delta)
            loaded = self.jobs.store.load("kucoin", "spot", "BTC/USDT", call["timeframe"])
            self.assertEqual(pd.Timestamp(loaded["timestamp"].iloc[-1]) + delta, call["end"])
        self.assertEqual(len(result["progress"]), 3)
        # Caller mutation must not change the manager's stored job.
        result["progress"].clear()
        self.assertEqual(len(self.jobs.get(result["job_id"])["progress"]), 3)

    def test_truncated_or_gapped_provider_history_is_never_saved_as_complete(self):
        for mode in ("truncated", "gapped"):
            def fetch(**kwargs):
                data = complete_history(**kwargs)
                return data.iloc[1:].reset_index(drop=True) if mode == "truncated" else data.drop(index=10).reset_index(drop=True)
            self.jobs.fetcher = fetch
            result = self.finish(self.jobs.start(self.request))
            self.assertEqual(result["state"], "error")
            self.assertEqual(result["progress"], [])
            self.assertFalse(list(self.root.rglob("*.csv")))

    def test_provider_failure_after_one_dataset_is_not_a_completed_job(self):
        def fetch(**kwargs):
            if kwargs["timeframe"] == "1h":
                raise RuntimeError("private credential/provider details")
            return complete_history(**kwargs)
        self.jobs.fetcher = fetch
        result = self.finish(self.jobs.start(self.request))
        self.assertEqual(result["state"], "error")
        self.assertEqual(len(result["progress"]), 1)
        self.assertNotIn("private", result["error"])
        self.assertEqual(len(list(self.root.rglob("*.csv"))), 1)

    def test_duplicate_job_rejected_and_shutdown_cancels_before_persistence(self):
        entered, release = Event(), Event()

        def fetch(**kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return complete_history(**kwargs)

        self.jobs.fetcher = fetch
        job = self.jobs.start(self.request)
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(HistoryJobConflict):
                self.jobs.start(self.request)
            self.assertFalse(self.jobs.shutdown(0))
        finally:
            release.set()
        result = self.finish(job)
        self.assertEqual(result["state"], "error")
        self.assertIn("stopped", result["message"])
        self.assertFalse(list(self.root.rglob("*.csv")))

    def test_invalid_or_future_period_creates_no_files(self):
        for start, end in (("2026-02-30", "2026-03-01"), ("2026-01-10", "2026-01-09"),
                           ("2020-01-01", "2026-01-10")):
            with self.assertRaises(ValueError):
                HistoryRequest("kucoin", "spot", "BTC/USDT", start, end)
        with self.assertRaises(ValueError):
            self.jobs.start(HistoryRequest("kucoin", "spot", "BTC/USDT", "2026-01-11", "2026-01-12"))
        self.assertFalse(self.root.exists())

    def test_api_preparation_returns_job_without_running_backtest(self):
        with patch.object(web, "_history_jobs", return_value=self.jobs), \
             patch.object(web, "run_decision_backtest", side_effect=AssertionError("No implicit backtest")), \
             TestClient(web.app) as client:
            response = client.post("/api/history/prepare", json=self.request.__dict__)
            self.assertEqual(response.status_code, 202, response.text)
            result = self.finish(response.json())
            self.assertEqual(client.get("/api/history/jobs/" + result["job_id"]).json()["state"], "completed")
            self.assertEqual(client.get("/api/history/jobs/not-an-id").status_code, 422)
            self.assertEqual(client.get("/api/history/jobs/00000000-0000-0000-0000-000000000000").status_code, 404)


if __name__ == "__main__":
    unittest.main()
