from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from trading_bot.analysis.journal import AnalysisJournal, JournalIntegrityError


def report(report_id="analysis-1", generated_at="2026-11-12T12:00:00+00:00"):
    return {
        "report_id": report_id,
        "generated_at": generated_at,
        "expires_at": "2026-11-12T12:15:00Z",
        "instrument": {"symbol": "QNT/USDT", "name": "Quant", "market_type": "spot"},
        "decision": {"action": "WAIT_FOR_TRIGGER", "score": 73, "win_probability": None},
        "technical": {"reasons": ["Entry confirmation pending."], "level": 102.5},
        "journal": {"saved": False},
    }


class AnalysisJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "analysis.sqlite"
        self.journal = AnalysisJournal(self.path)

    def corrupt(self, statement, values=()):
        # Simulate out-of-band storage corruption by removing the SQL guard.
        # The public API exposes no report mutation operation.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER analysis_reports_no_update")
            connection.execute(statement, values)

    def test_roundtrip_records_digest_without_mutating_the_caller(self):
        original = report()
        before = deepcopy(original)
        self.assertIsNone(self.journal.save(original))
        self.assertEqual(original, before)
        saved = self.journal.get("analysis-1")
        payload = {key: value for key, value in before.items() if key != "journal"}
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(saved["journal"], {
            "saved": True, "content_sha256": digest, "schema_version": 1,
        })
        self.assertEqual({key: value for key, value in saved.items() if key != "journal"}, payload)
        json.dumps(saved, allow_nan=False)
        original["technical"]["level"] = 0
        saved["technical"]["level"] = 1
        self.assertEqual(self.journal.get("analysis-1")["technical"]["level"], 102.5)

    def test_report_survives_reopen_with_persistent_schema_version(self):
        self.journal.save(report())
        self.assertEqual(AnalysisJournal(self.path).get("analysis-1"), self.journal.get("analysis-1"))
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute(
                "SELECT value FROM analysis_journal_metadata WHERE key = 'schema_version'"
            ).fetchone()
        self.assertEqual(stored, ("1",))

    def test_duplicate_id_raises_and_preserves_original(self):
        self.journal.save(report())
        duplicate = report()
        duplicate["decision"]["action"] = "LONG_READY"
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.save(duplicate)
        self.assertEqual(self.journal.get("analysis-1")["decision"]["action"], "WAIT_FOR_TRIGGER")
        self.assertEqual(len(self.journal.history()), 1)

    def test_database_guards_reject_updates_and_deletes(self):
        self.journal.save(report())
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in (
                "UPDATE analysis_reports SET generated_at = 'changed'",
                "DELETE FROM analysis_reports",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    with connection:
                        connection.execute(statement)
        self.assertIsNotNone(self.journal.get("analysis-1"))

    def test_tampered_payload_is_rejected_by_get_and_history(self):
        self.journal.save(report())
        changed = self.journal.get("analysis-1")
        changed["decision"]["score"] = 99
        self.corrupt("UPDATE analysis_reports SET report_json = ?", (json.dumps(changed),))
        with self.assertRaises(JournalIntegrityError):
            self.journal.get("analysis-1")
        with self.assertRaises(JournalIntegrityError):
            self.journal.history()

    def test_tampered_stored_digest_is_rejected(self):
        self.journal.save(report())
        self.corrupt("UPDATE analysis_reports SET content_sha256 = ?", ("0" * 64,))
        with self.assertRaises(JournalIntegrityError):
            self.journal.get("analysis-1")

    def test_tampered_journal_metadata_is_rejected(self):
        self.journal.save(report())
        changed = self.journal.get("analysis-1")
        changed["journal"]["saved"] = False
        self.corrupt("UPDATE analysis_reports SET report_json = ?", (json.dumps(changed),))
        with self.assertRaises(JournalIntegrityError):
            self.journal.get("analysis-1")

    def test_tampered_history_sort_metadata_is_rejected(self):
        self.journal.save(report())
        self.corrupt("UPDATE analysis_reports SET generated_us = generated_us + 1")
        with self.assertRaises(JournalIntegrityError):
            self.journal.history()

    def test_duplicate_keys_in_stored_json_are_rejected(self):
        self.journal.save(report())
        raw = json.dumps(self.journal.get("analysis-1"))
        raw = raw[:-1] + ', "report_id": "analysis-1"}'
        self.corrupt("UPDATE analysis_reports SET report_json = ?", (raw,))
        with self.assertRaises(JournalIntegrityError):
            self.journal.get("analysis-1")

    def test_history_is_bounded_and_returns_verified_summaries(self):
        for index in range(30):
            self.journal.save(report(f"report-{index:02d}", f"2026-11-12T12:00:{index:02d}Z"))
        self.assertEqual(len(self.journal.history()), 25)
        history = self.journal.history(limit=2)
        self.assertEqual([item["report_id"] for item in history], ["report-29", "report-28"])
        self.assertEqual(history[0], {
            "report_id": "report-29", "generated_at": "2026-11-12T12:00:29Z",
            "expires_at": "2026-11-12T12:15:00Z", "symbol": "QNT/USDT",
            "exchange": None, "market_type": "spot",
            "action": "WAIT_FOR_TRIGGER", "score": 73,
        })

    def test_history_orders_mixed_utc_forms_by_time_then_insertion(self):
        self.journal.save(report("second", "2026-11-12T12:00:00.1Z"))
        self.journal.save(report("first", "2026-11-12T12:00:00+00:00"))
        self.journal.save(report("third", "2026-11-12T12:00:00.100000+00:00"))
        self.assertEqual([item["report_id"] for item in self.journal.history()],
                         ["third", "second", "first"])

    def test_history_rejects_invalid_limits(self):
        for limit in (0, -1, 501, True, 1.5, "25", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.journal.history(limit)

    def test_missing_report_and_empty_history(self):
        self.assertIsNone(self.journal.get("missing"))
        self.assertEqual(self.journal.history(), [])

    def test_nullable_expiry_and_unresolved_instrument_can_be_saved(self):
        unresolved = report()
        unresolved["expires_at"] = None
        unresolved["instrument"] = None
        unresolved["decision"] = {"action": "INSUFFICIENT_DATA", "score": None}
        self.journal.save(unresolved)
        summary = self.journal.history()[0]
        self.assertIsNone(summary["expires_at"])
        self.assertIsNone(summary["symbol"])
        self.assertIsNone(summary["score"])
        self.assertEqual(summary["action"], "INSUFFICIENT_DATA")

    def test_independent_concurrent_clients_save_without_shared_connections(self):
        barrier = Barrier(8)

        def save_one(index):
            client = AnalysisJournal(self.path)
            barrier.wait(timeout=10)
            client.save(report(f"concurrent-{index}"))
            return client.get(f"concurrent-{index}")["report_id"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            stored = list(executor.map(save_one, range(24)))
        self.assertEqual(set(stored), {f"concurrent-{index}" for index in range(24)})
        self.assertEqual(len(self.journal.history(limit=500)), 24)

    def test_concurrent_duplicate_requests_insert_exactly_once(self):
        barrier = Barrier(8)

        def save_once(index):
            candidate = report()
            candidate["decision"]["score"] = index
            barrier.wait(timeout=10)
            try:
                self.journal.save(candidate)
                return True
            except sqlite3.IntegrityError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(save_once, range(8)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(self.journal.history()), 1)
        self.assertIsNotNone(self.journal.get("analysis-1"))

    def test_nonfinite_numbers_are_rejected_without_partial_insert(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            invalid = report()
            invalid["technical"]["level"] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                self.journal.save(invalid)
        self.assertEqual(self.journal.history(), [])

    def test_non_json_types_are_not_silently_stringified(self):
        invalid = report()
        invalid["technical"]["unsupported"] = datetime(2026, 11, 12)
        with self.assertRaises(TypeError):
            self.journal.save(invalid)
        self.assertEqual(self.journal.history(), [])

    def test_required_identity_and_utc_timestamps_are_validated(self):
        for field, invalid_value in (
            ("report_id", ""), ("report_id", None),
            ("generated_at", "2026-11-12T12:00:00"),
            ("generated_at", "2026-11-12T12:00:00+01:00"),
            ("generated_at", "invalid"), ("generated_at", None),
            ("expires_at", "2026-11-12T12:15:00"),
        ):
            invalid = report()
            invalid[field] = invalid_value
            with self.subTest(field=field, value=invalid_value), self.assertRaises(ValueError):
                self.journal.save(invalid)
        self.assertEqual(self.journal.history(), [])

    def test_unknown_schema_version_is_not_overwritten(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE analysis_journal_metadata SET value = '999' WHERE key = 'schema_version'"
            )
        with self.assertRaisesRegex(ValueError, "schema version"):
            AnalysisJournal(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT value FROM analysis_journal_metadata WHERE key = 'schema_version'"
            ).fetchone(), ("999",))

    def test_in_memory_path_is_rejected_for_per_operation_connections(self):
        with self.assertRaises(ValueError):
            AnalysisJournal(":memory:")


if __name__ == "__main__":
    unittest.main()
