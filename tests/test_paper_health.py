from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from trading_bot.paper.health import paper_health


NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def heartbeat(path, status="ok", finished=NOW, **extra):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE paper_service_metadata (key TEXT PRIMARY KEY, value TEXT)")
    payload = {"status": status, "last_cycle_finished_at": finished.isoformat(),
               "last_cycle_result": "ok" if status == "running" else status, **extra}
    connection.execute("INSERT INTO paper_service_metadata VALUES (?, ?)", ("heartbeat", json.dumps(payload)))
    connection.commit()
    connection.close()


def test_missing_health_does_not_create_a_journal(tmp_path):
    path = tmp_path / "missing.db"
    assert paper_health(path, now=NOW)["status"] == "missing"
    assert not path.exists()


@pytest.mark.parametrize("status,healthy", [("ok", True), ("running", True), ("degraded", False), ("error", False)])
def test_heartbeat_health_reports_degraded_cycles(tmp_path, status, healthy):
    path = tmp_path / "paper.db"
    heartbeat(path, status=status)
    before = path.read_bytes()
    result = paper_health(path, now=NOW + timedelta(seconds=10))
    assert result["healthy"] is healthy
    assert result["status"] == status
    assert path.read_bytes() == before


def test_fresh_process_with_stale_completed_cycle_is_unhealthy(tmp_path):
    path = tmp_path / "paper.db"
    heartbeat(path, status="running", finished=NOW - timedelta(hours=1), last_cycle_started_at=NOW.isoformat())
    assert paper_health(path, max_age_seconds=300, now=NOW)["status"] == "stale"


@pytest.mark.parametrize("previous_result", ["degraded", "error", None])
def test_starting_another_cycle_cannot_erase_previous_failure(tmp_path, previous_result):
    path = tmp_path / "paper.db"
    heartbeat(path, status="running", last_cycle_result=previous_result)
    result = paper_health(path, now=NOW)
    assert result["status"] == "running"
    assert result["healthy"] is False


@pytest.mark.parametrize("age", [float("nan"), float("inf"), 0, -1])
def test_invalid_health_age_is_rejected(tmp_path, age):
    with pytest.raises(ValueError):
        paper_health(tmp_path / "paper.db", age, now=NOW)
