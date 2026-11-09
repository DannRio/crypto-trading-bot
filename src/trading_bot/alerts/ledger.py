from pathlib import Path
import sqlite3

import pandas as pd


class AlertLedger:
    def __init__(
        self,
        path: str | Path = "data/alerts/alerts.db",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_state (
                fingerprint TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL,
                send_count INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()

    def should_emit(
        self,
        fingerprint: str,
        now,
        cooldown_seconds: int,
    ) -> bool:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative.")

        now_ts = pd.Timestamp(now)
        if now_ts.tzinfo is None:
            now_ts = now_ts.tz_localize("UTC")
        else:
            now_ts = now_ts.tz_convert("UTC")

        row = self.connection.execute(
            """
            SELECT last_sent_at
            FROM alert_state
            WHERE fingerprint = ?
            """,
            (fingerprint,),
        ).fetchone()

        if row is None:
            return True

        last = pd.Timestamp(row[0])
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        else:
            last = last.tz_convert("UTC")

        elapsed = (now_ts - last).total_seconds()
        return elapsed >= cooldown_seconds

    def record(
        self,
        fingerprint: str,
        sent_at,
    ) -> None:
        timestamp = pd.Timestamp(sent_at)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")

        self.connection.execute(
            """
            INSERT INTO alert_state (
                fingerprint,
                last_sent_at,
                send_count
            )
            VALUES (?, ?, 1)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_sent_at = excluded.last_sent_at,
                send_count = alert_state.send_count + 1
            """,
            (fingerprint, timestamp.isoformat()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
