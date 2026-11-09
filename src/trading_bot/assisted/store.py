from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pandas as pd

from trading_bot.assisted.models import (
    IntentDecision,
    IntentStatus,
    TradeIntent,
)


def _timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def plan_hash(plan: dict) -> str:
    return sha256(
        canonical_json(plan).encode("utf-8")
    ).hexdigest()


class IntentStore:
    def __init__(
        self,
        path: str | Path = "data/assisted/assisted.db",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trade_intents (
                intent_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                plan_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                signal_time TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                exchange_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy TEXT NOT NULL,
                signal TEXT NOT NULL,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                actor TEXT NOT NULL,
                note TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                FOREIGN KEY(intent_id)
                    REFERENCES trade_intents(intent_id)
            );

            CREATE INDEX IF NOT EXISTS idx_trade_intents_status
                ON trade_intents(status, expires_at);
            """
        )
        self.connection.commit()

    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> TradeIntent:
        plan = json.loads(row["plan_json"])
        actual_hash = plan_hash(plan)
        if actual_hash != row["plan_hash"]:
            raise ValueError(
                "Trade intent plan integrity check failed: "
                f"{row['intent_id']}"
            )
        return TradeIntent(
            intent_id=str(row["intent_id"]),
            fingerprint=str(row["fingerprint"]),
            plan_hash=str(row["plan_hash"]),
            created_at=_timestamp(row["created_at"]),
            signal_time=_timestamp(row["signal_time"]),
            expires_at=_timestamp(row["expires_at"]),
            exchange_id=str(row["exchange_id"]),
            market_type=str(row["market_type"]),
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            strategy=str(row["strategy"]),
            signal=str(row["signal"]),
            score=int(row["score"]),
            status=IntentStatus(str(row["status"])),
            plan=plan,
        )

    def queue(self, intent: TradeIntent) -> bool:
        if plan_hash(intent.plan) != intent.plan_hash:
            raise ValueError(
                "Intent plan hash does not match immutable plan."
            )

        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO trade_intents (
                intent_id,
                fingerprint,
                plan_hash,
                created_at,
                signal_time,
                expires_at,
                exchange_id,
                market_type,
                symbol,
                timeframe,
                strategy,
                signal,
                score,
                status,
                plan_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_id,
                intent.fingerprint,
                intent.plan_hash,
                intent.created_at.isoformat(),
                intent.signal_time.isoformat(),
                intent.expires_at.isoformat(),
                intent.exchange_id,
                intent.market_type,
                intent.symbol,
                intent.timeframe,
                intent.strategy,
                intent.signal,
                intent.score,
                intent.status.value,
                canonical_json(intent.plan),
                intent.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def get(self, intent_id: str) -> TradeIntent | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM trade_intents
            WHERE intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        return (
            self._row_to_intent(row)
            if row is not None
            else None
        )

    def expire_stale(self, now=None) -> int:
        timestamp = (
            pd.Timestamp.now(tz="UTC")
            if now is None
            else _timestamp(now)
        )
        rows = self.connection.execute(
            """
            SELECT intent_id
            FROM trade_intents
            WHERE status IN ('PENDING_APPROVAL', 'APPROVED')
              AND expires_at <= ?
            """,
            (timestamp.isoformat(),),
        ).fetchall()

        for row in rows:
            intent_id = str(row["intent_id"])
            self.connection.execute(
                """
                UPDATE trade_intents
                SET status = 'EXPIRED',
                    updated_at = ?
                WHERE intent_id = ?
                """,
                (timestamp.isoformat(), intent_id),
            )
            self.connection.execute(
                """
                INSERT INTO intent_decisions (
                    intent_id,
                    decision,
                    actor,
                    note,
                    decided_at
                )
                VALUES (?, 'EXPIRED', 'SYSTEM', ?, ?)
                """,
                (
                    intent_id,
                    "Intent expired before execution.",
                    timestamp.isoformat(),
                ),
            )

        self.connection.commit()
        return len(rows)

    def list(
        self,
        statuses: tuple[IntentStatus, ...] | None = None,
        limit: int = 100,
        expire_at=None,
    ) -> list[TradeIntent]:
        if limit <= 0:
            raise ValueError("limit must be positive.")
        self.expire_stale(expire_at)

        if statuses:
            placeholders = ",".join(
                "?"
                for _ in statuses
            )
            rows = self.connection.execute(
                f"""
                SELECT *
                FROM trade_intents
                WHERE status IN ({placeholders})
                ORDER BY signal_time DESC
                LIMIT ?
                """,
                tuple(status.value for status in statuses)
                + (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM trade_intents
                ORDER BY signal_time DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            self._row_to_intent(row)
            for row in rows
        ]

    def _decide(
        self,
        intent_id: str,
        new_status: IntentStatus,
        actor: str,
        note: str = "",
        decided_at=None,
    ) -> TradeIntent:
        actor = actor.strip()
        if not actor:
            raise ValueError("Decision actor is required.")

        timestamp = (
            pd.Timestamp.now(tz="UTC")
            if decided_at is None
            else _timestamp(decided_at)
        )
        self.expire_stale(timestamp)
        intent = self.get(intent_id)
        if intent is None:
            raise KeyError(f"Unknown intent: {intent_id}")
        if intent.status != IntentStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Intent {intent_id} is {intent.status.value}; "
                "only PENDING_APPROVAL intents can be decided."
            )

        self.connection.execute(
            """
            UPDATE trade_intents
            SET status = ?,
                updated_at = ?
            WHERE intent_id = ?
            """,
            (
                new_status.value,
                timestamp.isoformat(),
                intent_id,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO intent_decisions (
                intent_id,
                decision,
                actor,
                note,
                decided_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                new_status.value,
                actor,
                note,
                timestamp.isoformat(),
            ),
        )
        self.connection.commit()

        updated = self.get(intent_id)
        assert updated is not None
        return updated

    def approve(
        self,
        intent_id: str,
        actor: str,
        note: str = "",
        decided_at=None,
    ) -> TradeIntent:
        return self._decide(
            intent_id,
            IntentStatus.APPROVED,
            actor,
            note,
            decided_at,
        )

    def reject(
        self,
        intent_id: str,
        actor: str,
        note: str = "",
        decided_at=None,
    ) -> TradeIntent:
        return self._decide(
            intent_id,
            IntentStatus.REJECTED,
            actor,
            note,
            decided_at,
        )

    def decisions(
        self,
        intent_id: str | None = None,
        limit: int = 100,
    ) -> list[IntentDecision]:
        if intent_id is None:
            rows = self.connection.execute(
                """
                SELECT intent_id, decision, actor, note, decided_at
                FROM intent_decisions
                ORDER BY decided_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT intent_id, decision, actor, note, decided_at
                FROM intent_decisions
                WHERE intent_id = ?
                ORDER BY decided_at DESC
                LIMIT ?
                """,
                (intent_id, limit),
            ).fetchall()

        return [
            IntentDecision(
                intent_id=str(row["intent_id"]),
                decision=str(row["decision"]),
                actor=str(row["actor"]),
                note=str(row["note"]),
                decided_at=_timestamp(row["decided_at"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()


def new_intent_id() -> str:
    return uuid4().hex
