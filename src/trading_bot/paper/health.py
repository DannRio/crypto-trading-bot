"""Read-only paper-daemon health probe for local and container monitoring."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3


def paper_health(journal: str | Path, max_age_seconds: float = 1800,
                 now: datetime | None = None) -> dict:
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be finite and positive.")
    path = Path(journal).expanduser().resolve()
    result = {"healthy": False, "status": "missing", "journal": str(path)}
    if not path.is_file():
        return result
    try:
        # mode=ro prevents a status request from creating or changing a journal.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT value FROM paper_service_metadata WHERE key = 'heartbeat'"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return dict(result, status="uninitialized")
        heartbeat = json.loads(row[0])
        if not isinstance(heartbeat, dict):
            raise ValueError("Invalid heartbeat record.")
        result["heartbeat"] = heartbeat
        finished = heartbeat.get("last_cycle_finished_at")
        if not finished:
            return dict(result, status="starting")
        timestamp = datetime.fromisoformat(finished)
        if timestamp.tzinfo is None:
            raise ValueError("Heartbeat timestamp has no timezone.")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("Health-check time must include a timezone.")
        age = (current - timestamp).total_seconds()
        result["age_seconds"] = age
        if age < -5:
            return dict(result, status="clock_error")
        if age > max_age_seconds:
            return dict(result, status="stale")
        status = heartbeat.get("status", "unknown")
        return dict(result, healthy=status in {"ok", "running"}, status=status)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return dict(result, status="error", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--journal", default="data/paper/continuous_paper.db")
    parser.add_argument("--max-age-seconds", type=float, default=1800)
    args = parser.parse_args(argv)
    try:
        status = paper_health(args.journal, args.max_age_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(status, sort_keys=True))
    return 0 if status["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
