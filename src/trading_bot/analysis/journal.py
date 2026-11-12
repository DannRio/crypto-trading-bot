"""Append-only storage for analysis snapshots, without trade-outcome claims.

Reports are strict JSON.  SHA-256 covers the canonical report without its
``journal`` object; the digest detects inconsistent storage, not an independently
authenticated signature.  Each operation opens and closes its own connection.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 10.0
MAX_HISTORY_LIMIT = 500


class JournalIntegrityError(ValueError):
    """A stored report or its indexed metadata failed verification."""


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are not supported.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON numbers are not supported.")


def _read_json(document):
    return json.loads(document, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _utc_microseconds(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UTC ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a UTC ISO timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC ISO timestamp.")
    elapsed = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _identity(report):
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or not report_id.strip() or len(report_id) > 200:
        raise ValueError("A nonempty report_id of up to 200 characters is required.")
    generated_at = report.get("generated_at")
    generated_us = _utc_microseconds(generated_at, "generated_at")
    expires_at = report.get("expires_at")
    if expires_at is not None:
        _utc_microseconds(expires_at, "expires_at")
    return report_id, generated_at, expires_at, generated_us


class AnalysisJournal:
    """Persist immutable reports in an on-disk SQLite database.

    ``save(report)`` does not mutate its argument.  It replaces supplied journal
    metadata in the persisted copy with ``saved``, ``content_sha256``, and
    ``schema_version``.  Call ``get(report_id)`` for that verified copy.  Duplicate
    IDs raise ``sqlite3.IntegrityError``; existing reports are never replaced.

    ``history(limit=25)`` accepts 1 through 500 and verifies every returned
    snapshot before constructing its summary.  Ordering is newest generation
    time first, then newest insertion for equal generation times.
    """

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("AnalysisJournal requires a persistent database path.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_journal_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO analysis_journal_metadata (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            version = connection.execute(
                "SELECT value FROM analysis_journal_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is None or version["value"] != str(SCHEMA_VERSION):
                raise ValueError("Unsupported analysis journal schema version.")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_reports (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL UNIQUE,
                    generated_at TEXT NOT NULL,
                    expires_at TEXT,
                    generated_us INTEGER NOT NULL,
                    report_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_analysis_reports_generated
                    ON analysis_reports (generated_us DESC, sequence DESC)
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS analysis_reports_no_update
                BEFORE UPDATE ON analysis_reports
                BEGIN
                    SELECT RAISE(ABORT, 'Analysis reports are append-only.');
                END
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS analysis_reports_no_delete
                BEFORE DELETE ON analysis_reports
                BEGIN
                    SELECT RAISE(ABORT, 'Analysis reports are append-only.');
                END
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=LOCK_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, report: dict) -> None:
        if not isinstance(report, dict):
            raise TypeError("An analysis report must be a JSON object.")
        # A serialization round trip isolates nested containers and validates
        # strict JSON before opening a write transaction.
        copied = _read_json(_canonical_json(report))
        copied.pop("journal", None)
        identity = _identity(copied)
        digest = sha256(_canonical_json(copied).encode("utf-8")).hexdigest()
        copied["journal"] = {
            "saved": True,
            "content_sha256": digest,
            "schema_version": SCHEMA_VERSION,
        }
        document = _canonical_json(copied)
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO analysis_reports (
                    report_id, generated_at, expires_at, generated_us,
                    report_json, content_sha256, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (*identity, document, digest, SCHEMA_VERSION))

    @staticmethod
    def _verified_report(row) -> dict:
        try:
            report = _read_json(row["report_json"])
            if not isinstance(report, dict):
                raise ValueError("Report is not an object.")
            journal = report.get("journal")
            if not isinstance(journal, dict) or journal.get("saved") is not True:
                raise ValueError("Journal metadata is missing.")
            if (type(journal.get("schema_version")) is not int
                    or journal["schema_version"] != SCHEMA_VERSION
                    or row["schema_version"] != SCHEMA_VERSION):
                raise ValueError("Unexpected report schema version.")
            payload = {key: value for key, value in report.items() if key != "journal"}
            digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            if not isinstance(journal.get("content_sha256"), str):
                raise ValueError("Missing report digest.")
            if (not compare_digest(digest, row["content_sha256"])
                    or not compare_digest(digest, journal["content_sha256"])):
                raise ValueError("Report content digest does not match.")
            expected = (row["report_id"], row["generated_at"], row["expires_at"], row["generated_us"])
            if _identity(payload) != expected:
                raise ValueError("Indexed report metadata does not match.")
            return report
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise JournalIntegrityError("Saved analysis report failed its integrity check.") from exc

    def get(self, report_id: str) -> dict | None:
        if not isinstance(report_id, str) or not report_id:
            raise ValueError("A report_id is required.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        return self._verified_report(row) if row is not None else None

    def history(self, limit: int = 25) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"History limit must be an integer from 1 to {MAX_HISTORY_LIMIT}.")
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT * FROM analysis_reports
                ORDER BY generated_us DESC, sequence DESC
                LIMIT ?
            """, (limit,)).fetchall()
        summaries = []
        for row in rows:
            report = self._verified_report(row)
            instrument = report.get("instrument")
            instrument = instrument if isinstance(instrument, dict) else {}
            decision = report.get("decision")
            decision = decision if isinstance(decision, dict) else {}
            summaries.append({
                "report_id": report["report_id"],
                "generated_at": report["generated_at"],
                "expires_at": report.get("expires_at"),
                "symbol": instrument.get("symbol", report.get("symbol")),
                "exchange": instrument.get("exchange", report.get("exchange")),
                "market_type": instrument.get("market_type", report.get("market_type")),
                "action": decision.get("action", report.get("action")),
                "score": decision.get("score", report.get("score")),
            })
        return summaries
