from __future__ import annotations

from pathlib import Path
import sqlite3
from uuid import uuid4

import pandas as pd

from trading_bot.assisted import PreparedOrder
from trading_bot.execution.models import (
    ManagedOrder,
    ManagedOrderStatus,
    OrderEvent,
    TERMINAL_ORDER_STATUSES,
)


def _utc_timestamp(value=None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


class OrderStore:
    def __init__(
        self,
        path: str | Path = "data/orders/orders.db",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.connection = sqlite3.connect(
            self.path
        )
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS managed_orders (
                order_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL UNIQUE,
                exchange_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                limit_price REAL NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                external_order_id TEXT,
                filled_amount REAL NOT NULL,
                average_price REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY(order_id)
                    REFERENCES managed_orders(order_id)
            );

            CREATE INDEX IF NOT EXISTS idx_managed_orders_status
                ON managed_orders(status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_order_events_order
                ON order_events(order_id, occurred_at);
            """
        )
        self.connection.commit()

    @staticmethod
    def _row_to_order(
        row: sqlite3.Row,
    ) -> ManagedOrder:
        return ManagedOrder(
            order_id=str(row["order_id"]),
            intent_id=str(row["intent_id"]),
            exchange_id=str(row["exchange_id"]),
            market_type=str(row["market_type"]),
            symbol=str(row["symbol"]),
            order_type=str(row["order_type"]),
            side=str(row["side"]),
            amount=float(row["amount"]),
            limit_price=float(row["limit_price"]),
            client_order_id=str(
                row["client_order_id"]
            ),
            status=ManagedOrderStatus(
                str(row["status"])
            ),
            external_order_id=(
                str(row["external_order_id"])
                if row["external_order_id"] is not None
                else None
            ),
            filled_amount=float(
                row["filled_amount"]
            ),
            average_price=(
                float(row["average_price"])
                if row["average_price"] is not None
                else None
            ),
            created_at=_utc_timestamp(
                row["created_at"]
            ),
            updated_at=_utc_timestamp(
                row["updated_at"]
            ),
            last_error=(
                str(row["last_error"])
                if row["last_error"] is not None
                else None
            ),
        )

    def get(
        self,
        order_id: str,
    ) -> ManagedOrder | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM managed_orders
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        return (
            self._row_to_order(row)
            if row is not None
            else None
        )

    def get_by_intent(
        self,
        intent_id: str,
    ) -> ManagedOrder | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM managed_orders
            WHERE intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        return (
            self._row_to_order(row)
            if row is not None
            else None
        )

    def create_or_get(
        self,
        prepared: PreparedOrder,
        now=None,
    ) -> tuple[ManagedOrder, bool]:
        existing = self.get_by_intent(
            prepared.intent_id
        )
        if existing is not None:
            expected = (
                prepared.exchange_id,
                prepared.market_type,
                prepared.symbol,
                prepared.order_type,
                prepared.side,
                float(prepared.amount),
                float(prepared.price),
                prepared.client_order_id,
            )
            actual = (
                existing.exchange_id,
                existing.market_type,
                existing.symbol,
                existing.order_type,
                existing.side,
                existing.amount,
                existing.limit_price,
                existing.client_order_id,
            )
            if actual != expected:
                raise ValueError(
                    "Existing managed order does not match "
                    "the immutable prepared order."
                )
            return existing, False

        timestamp = _utc_timestamp(now)
        order_id = uuid4().hex

        try:
            self.connection.execute(
                """
                INSERT INTO managed_orders (
                    order_id,
                    intent_id,
                    exchange_id,
                    market_type,
                    symbol,
                    order_type,
                    side,
                    amount,
                    limit_price,
                    client_order_id,
                    status,
                    external_order_id,
                    filled_amount,
                    average_price,
                    created_at,
                    updated_at,
                    last_error
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'PREPARED', NULL, 0, NULL, ?, ?, NULL
                )
                """,
                (
                    order_id,
                    prepared.intent_id,
                    prepared.exchange_id,
                    prepared.market_type,
                    prepared.symbol,
                    prepared.order_type,
                    prepared.side,
                    float(prepared.amount),
                    float(prepared.price),
                    prepared.client_order_id,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO order_events (
                    order_id,
                    event_type,
                    status,
                    occurred_at,
                    message
                )
                VALUES (?, 'PREPARED', 'PREPARED', ?, ?)
                """,
                (
                    order_id,
                    timestamp.isoformat(),
                    "Prepared order persisted before submission.",
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            self.connection.rollback()
            existing = self.get_by_intent(
                prepared.intent_id
            )
            if existing is None:
                raise
            return existing, False

        created = self.get(order_id)
        assert created is not None
        return created, True

    def transition(
        self,
        order_id: str,
        new_status: ManagedOrderStatus,
        allowed_from: tuple[
            ManagedOrderStatus,
            ...,
        ],
        *,
        external_order_id: str | None = None,
        filled_amount: float | None = None,
        average_price: float | None = None,
        last_error: str | None = None,
        event_type: str | None = None,
        message: str = "",
        now=None,
    ) -> ManagedOrder:
        if not allowed_from:
            raise ValueError(
                "allowed_from cannot be empty."
            )

        current = self.get(order_id)
        if current is None:
            raise KeyError(
                f"Unknown managed order: {order_id}"
            )
        if current.status not in allowed_from:
            raise ValueError(
                f"Order {order_id} is {current.status.value}; "
                f"cannot transition to {new_status.value}."
            )

        timestamp = _utc_timestamp(now)
        next_external = (
            external_order_id
            if external_order_id is not None
            else current.external_order_id
        )
        next_filled = (
            float(filled_amount)
            if filled_amount is not None
            else current.filled_amount
        )
        next_average = (
            float(average_price)
            if average_price is not None
            else current.average_price
        )

        if next_filled < 0 or next_filled > current.amount:
            raise ValueError(
                "filled_amount must be within order amount."
            )

        placeholders = ",".join(
            "?"
            for _ in allowed_from
        )
        params = (
            new_status.value,
            next_external,
            next_filled,
            next_average,
            timestamp.isoformat(),
            last_error,
            order_id,
            *(
                status.value
                for status in allowed_from
            ),
        )

        try:
            cursor = self.connection.execute(
                f"""
                UPDATE managed_orders
                SET status = ?,
                    external_order_id = ?,
                    filled_amount = ?,
                    average_price = ?,
                    updated_at = ?,
                    last_error = ?
                WHERE order_id = ?
                  AND status IN ({placeholders})
                """,
                params,
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise RuntimeError(
                    "Order state changed concurrently; "
                    "reload before retrying."
                )

            self.connection.execute(
                """
                INSERT INTO order_events (
                    order_id,
                    event_type,
                    status,
                    occurred_at,
                    message
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    event_type
                    or new_status.value,
                    new_status.value,
                    timestamp.isoformat(),
                    message,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        updated = self.get(order_id)
        assert updated is not None
        return updated

    def events(
        self,
        order_id: str,
        limit: int = 100,
    ) -> list[OrderEvent]:
        rows = self.connection.execute(
            """
            SELECT order_id, event_type, status, occurred_at, message
            FROM order_events
            WHERE order_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (order_id, limit),
        ).fetchall()
        return [
            OrderEvent(
                order_id=str(row["order_id"]),
                event_type=str(row["event_type"]),
                status=ManagedOrderStatus(
                    str(row["status"])
                ),
                occurred_at=_utc_timestamp(
                    row["occurred_at"]
                ),
                message=str(row["message"]),
            )
            for row in rows
        ]

    def active_orders(
        self,
    ) -> list[ManagedOrder]:
        terminal = tuple(
            status.value
            for status in TERMINAL_ORDER_STATUSES
        )
        placeholders = ",".join(
            "?"
            for _ in terminal
        )
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM managed_orders
            WHERE status NOT IN ({placeholders})
            ORDER BY created_at ASC
            """,
            terminal,
        ).fetchall()
        return [
            self._row_to_order(row)
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()
