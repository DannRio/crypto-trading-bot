from pathlib import Path
from dataclasses import asdict
import sqlite3

import pandas as pd

from trading_bot.paper.models import PaperOrder, PaperTrade
from trading_bot.paper.risk import PortfolioRiskState


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
                entry_low REAL NOT NULL DEFAULT 0,
                entry_high REAL NOT NULL DEFAULT 0,
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
                exit_reason TEXT,
                take_profit_1_fraction REAL NOT NULL DEFAULT 0.5,
                move_stop_to_break_even_after_tp1 INTEGER NOT NULL DEFAULT 1,
                tp1_hit INTEGER NOT NULL DEFAULT 0,
                remaining_size REAL,
                realized_gross_pnl REAL NOT NULL DEFAULT 0,
                realized_exit_fees REAL NOT NULL DEFAULT 0,
                realized_exit_value REAL NOT NULL DEFAULT 0,
                effective_stop REAL
            );

            CREATE TABLE IF NOT EXISTS paper_risk_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                peak_equity REAL NOT NULL,
                last_equity REAL NOT NULL,
                evaluated_at TEXT NOT NULL,
                day_key TEXT NOT NULL,
                day_start_equity REAL NOT NULL,
                week_key TEXT NOT NULL,
                week_start_equity REAL NOT NULL,
                halted INTEGER NOT NULL,
                halt_reason TEXT NOT NULL,
                halted_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_service_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
                strategy TEXT NOT NULL DEFAULT 'trend_continuation',
                tp1_hit INTEGER NOT NULL DEFAULT 0
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
        order_columns = {
            "event_id": "TEXT", "setup_type": "TEXT NOT NULL DEFAULT 'legacy_state'",
            "cancel_price": "REAL", "break_even_mode": "TEXT NOT NULL DEFAULT 'price'",
            "risk_budget": "REAL NOT NULL DEFAULT 0",
            "entry_low": "REAL NOT NULL DEFAULT 0",
            "entry_high": "REAL NOT NULL DEFAULT 0",
            "take_profit_1_fraction": "REAL NOT NULL DEFAULT 0.5",
            "move_stop_to_break_even_after_tp1": (
                "INTEGER NOT NULL DEFAULT 1"
            ),
            "tp1_hit": "INTEGER NOT NULL DEFAULT 0",
            "remaining_size": "REAL",
            "realized_gross_pnl": "REAL NOT NULL DEFAULT 0",
            "realized_exit_fees": "REAL NOT NULL DEFAULT 0",
            "realized_exit_value": "REAL NOT NULL DEFAULT 0",
            "effective_stop": "REAL",
        }
        for column, definition in order_columns.items():
            self._ensure_column(
                "paper_orders",
                column,
                definition,
            )
        self._ensure_column(
            "paper_trades",
            "tp1_hit",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column("paper_trades", "event_id", "TEXT")
        self._ensure_column("paper_trades", "setup_type", "TEXT NOT NULL DEFAULT 'legacy_state'")
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
        values = asdict(order)
        for key in ("signal", "status"):
            values[key] = values[key].value
        for key in ("created_at", "filled_at", "closed_at"):
            values[key] = self._timestamp(values[key])
        columns = list(values)
        updates = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "order_id")
        self.connection.execute(
            f"INSERT INTO paper_orders ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(order_id) DO UPDATE SET {updates}", tuple(values.values()))
        self.connection.commit()

    def has_setup_event(self, symbol, timeframe, event_id):
        return self.connection.execute(
            "SELECT 1 FROM paper_orders WHERE symbol=? AND timeframe=? AND event_id=? LIMIT 1",
            (symbol, timeframe, event_id)).fetchone() is not None

    def save_trade(self, trade: PaperTrade) -> None:
        self.connection.execute(
            """
            INSERT INTO paper_trades (
                order_id, symbol, timeframe, signal,
                entry_time, exit_time, entry_price, exit_price,
                position_size, gross_pnl, fees, net_pnl,
                risk_amount, r_multiple, exit_reason, score, regime,
                strategy, tp1_hit, event_id, setup_type
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
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
                int(trade.tp1_hit), trade.event_id, trade.setup_type,
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







    def load_risk_state(self) -> PortfolioRiskState | None:
        row = self.connection.execute(
            """
            SELECT
                peak_equity,
                last_equity,
                evaluated_at,
                day_key,
                day_start_equity,
                week_key,
                week_start_equity,
                halted,
                halt_reason,
                halted_at
            FROM paper_risk_state
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None

        halted_at = (
            pd.Timestamp(row[9])
            if row[9] is not None
            else None
        )
        return PortfolioRiskState(
            peak_equity=float(row[0]),
            last_equity=float(row[1]),
            evaluated_at=pd.Timestamp(row[2]),
            day_key=str(row[3]),
            day_start_equity=float(row[4]),
            week_key=str(row[5]),
            week_start_equity=float(row[6]),
            halted=bool(row[7]),
            halt_reason=str(row[8]),
            halted_at=halted_at,
        )

    def save_risk_state(
        self,
        state: PortfolioRiskState,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO paper_risk_state (
                id,
                peak_equity,
                last_equity,
                evaluated_at,
                day_key,
                day_start_equity,
                week_key,
                week_start_equity,
                halted,
                halt_reason,
                halted_at,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                peak_equity = excluded.peak_equity,
                last_equity = excluded.last_equity,
                evaluated_at = excluded.evaluated_at,
                day_key = excluded.day_key,
                day_start_equity = excluded.day_start_equity,
                week_key = excluded.week_key,
                week_start_equity = excluded.week_start_equity,
                halted = excluded.halted,
                halt_reason = excluded.halt_reason,
                halted_at = excluded.halted_at,
                updated_at = excluded.updated_at
            """,
            (
                state.peak_equity,
                state.last_equity,
                self._timestamp(state.evaluated_at),
                state.day_key,
                state.day_start_equity,
                state.week_key,
                state.week_start_equity,
                int(state.halted),
                state.halt_reason,
                self._timestamp(state.halted_at),
                pd.Timestamp.now(tz="UTC").isoformat(),
            ),
        )
        self.connection.commit()

    def clear_risk_state(self) -> None:
        self.connection.execute(
            "DELETE FROM paper_risk_state WHERE id = 1"
        )
        self.connection.commit()


    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT value
            FROM paper_service_metadata
            WHERE key = ?
            """,
            (key,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def set_metadata(
        self,
        key: str,
        value,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO paper_service_metadata (
                key,
                value
            )
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value
            """,
            (key, str(value)),
        )
        self.connection.commit()

    def metadata(self) -> dict[str, str]:
        return {
            str(key): str(value)
            for key, value in self.connection.execute(
                """
                SELECT key, value
                FROM paper_service_metadata
                ORDER BY key
                """
            ).fetchall()
        }


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
