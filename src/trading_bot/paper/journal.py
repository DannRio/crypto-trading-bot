from pathlib import Path
import sqlite3

import pandas as pd

from trading_bot.paper.models import PaperOrder, PaperTrade


class PaperJournal:
    def __init__(
        self,
        path: str | Path = "data/paper/paper_trading.db",
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                signal TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reference_entry REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit_1 REAL NOT NULL,
                take_profit_2 REAL NOT NULL,
                position_size REAL NOT NULL,
                planned_risk REAL NOT NULL,
                score INTEGER NOT NULL,
                regime TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'trend_continuation',
                bars_remaining INTEGER NOT NULL,
                filled_at TEXT,
                fill_price REAL,
                entry_fee REAL NOT NULL,
                closed_at TEXT,
                exit_price REAL,
                exit_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_service_checkpoints (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                last_processed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe)
            );

            CREATE TABLE IF NOT EXISTS paper_signal_events (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy TEXT NOT NULL,
                signal TEXT NOT NULL,
                signal_time TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    symbol,
                    timeframe,
                    strategy,
                    signal,
                    signal_time
                )
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                signal TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                position_size REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                fees REAL NOT NULL,
                net_pnl REAL NOT NULL,
                risk_amount REAL NOT NULL,
                r_multiple REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                score INTEGER NOT NULL,
                regime TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'trend_continuation'
            );
            """
        )
        self._ensure_column(
            "paper_orders",
            "strategy",
            "TEXT NOT NULL DEFAULT 'trend_continuation'",
        )
        self._ensure_column(
            "paper_trades",
            "strategy",
            "TEXT NOT NULL DEFAULT 'trend_continuation'",
        )
        self.connection.commit()

    def _ensure_column(
        self,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row[1]
            for row in self.connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN {column} {definition}"
            )

    @staticmethod
    def _timestamp(value) -> str | None:
        if value is None:
            return None
        return pd.Timestamp(value).isoformat()

    def save_order(self, order: PaperOrder) -> None:
        self.connection.execute(
            """
            INSERT INTO paper_orders (
                order_id, symbol, timeframe, signal, status,
                created_at, reference_entry, stop_loss,
                take_profit_1, take_profit_2, position_size,
                planned_risk, score, regime, strategy,
                bars_remaining, filled_at, fill_price, entry_fee,
                closed_at, exit_price, exit_reason
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(order_id) DO UPDATE SET
                status=excluded.status,
                bars_remaining=excluded.bars_remaining,
                filled_at=excluded.filled_at,
                fill_price=excluded.fill_price,
                entry_fee=excluded.entry_fee,
                closed_at=excluded.closed_at,
                exit_price=excluded.exit_price,
                exit_reason=excluded.exit_reason
            """,
            (
                order.order_id,
                order.symbol,
                order.timeframe,
                order.signal.value,
                order.status.value,
                self._timestamp(order.created_at),
                order.reference_entry,
                order.stop_loss,
                order.take_profit_1,
                order.take_profit_2,
                order.position_size,
                order.planned_risk,
                order.score,
                order.regime,
                order.strategy,
                order.bars_remaining,
                self._timestamp(order.filled_at),
                order.fill_price,
                order.entry_fee,
                self._timestamp(order.closed_at),
                order.exit_price,
                order.exit_reason,
            ),
        )
        self.connection.commit()

    def save_trade(self, trade: PaperTrade) -> None:
        self.connection.execute(
            """
            INSERT INTO paper_trades (
                order_id, symbol, timeframe, signal,
                entry_time, exit_time, entry_price, exit_price,
                position_size, gross_pnl, fees, net_pnl,
                risk_amount, r_multiple, exit_reason, score, regime,
                strategy
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.order_id,
                trade.symbol,
                trade.timeframe,
                trade.signal.value,
                self._timestamp(trade.entry_time),
                self._timestamp(trade.exit_time),
                trade.entry_price,
                trade.exit_price,
                trade.position_size,
                trade.gross_pnl,
                trade.fees,
                trade.net_pnl,
                trade.risk_amount,
                trade.r_multiple,
                trade.exit_reason,
                trade.score,
                trade.regime,
                trade.strategy,
            ),
        )
        self.connection.commit()

    def orders(self, limit: int = 100) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_orders
            ORDER BY created_at DESC
            LIMIT ?
            """,
            self.connection,
            params=(limit,),
        )

    def trades(self, limit: int = 100) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_trades
            ORDER BY exit_time DESC
            LIMIT ?
            """,
            self.connection,
            params=(limit,),
        )



    def active_orders(self) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_orders
            WHERE status IN ('PENDING', 'FILLED')
            ORDER BY created_at ASC
            """,
            self.connection,
        )

    def all_trades(self) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_trades
            ORDER BY exit_time ASC
            """,
            self.connection,
        )

    def get_checkpoint(
        self,
        symbol: str,
        timeframe: str,
    ) -> pd.Timestamp | None:
        row = self.connection.execute(
            """
            SELECT last_processed_at
            FROM paper_service_checkpoints
            WHERE symbol = ? AND timeframe = ?
            """,
            (symbol, timeframe),
        ).fetchone()
        if row is None:
            return None
        return pd.Timestamp(row[0])

    def set_checkpoint(
        self,
        symbol: str,
        timeframe: str,
        timestamp,
    ) -> None:
        value = self._timestamp(timestamp)
        now = pd.Timestamp.now(tz="UTC").isoformat()
        self.connection.execute(
            """
            INSERT INTO paper_service_checkpoints (
                symbol,
                timeframe,
                last_processed_at,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe) DO UPDATE SET
                last_processed_at = excluded.last_processed_at,
                updated_at = excluded.updated_at
            """,
            (symbol, timeframe, value, now),
        )
        self.connection.commit()

    def signal_seen(
        self,
        symbol: str,
        timeframe: str,
        strategy: str,
        signal: str,
        signal_time,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM paper_signal_events
            WHERE symbol = ?
              AND timeframe = ?
              AND strategy = ?
              AND signal = ?
              AND signal_time = ?
            """,
            (
                symbol,
                timeframe,
                strategy,
                signal,
                self._timestamp(signal_time),
            ),
        ).fetchone()
        return row is not None

    def record_signal(
        self,
        symbol: str,
        timeframe: str,
        strategy: str,
        signal: str,
        signal_time,
        action: str,
        reason: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO paper_signal_events (
                symbol,
                timeframe,
                strategy,
                signal,
                signal_time,
                action,
                reason,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                timeframe,
                strategy,
                signal,
                self._timestamp(signal_time),
                action,
                reason,
                pd.Timestamp.now(tz="UTC").isoformat(),
            ),
        )
        self.connection.commit()

    def checkpoints(self) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_service_checkpoints
            ORDER BY symbol, timeframe
            """,
            self.connection,
        )

    def signal_events(
        self,
        limit: int = 100,
    ) -> pd.DataFrame:
        return pd.read_sql_query(
            """
            SELECT * FROM paper_signal_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            self.connection,
            params=(limit,),
        )


    def close(self) -> None:
        self.connection.close()
