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
                bars_remaining INTEGER NOT NULL,
                filled_at TEXT,
                fill_price REAL,
                entry_fee REAL NOT NULL,
                closed_at TEXT,
                exit_price REAL,
                exit_reason TEXT
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
                regime TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

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
                planned_risk, score, regime, bars_remaining,
                filled_at, fill_price, entry_fee, closed_at,
                exit_price, exit_reason
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
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
                risk_amount, r_multiple, exit_reason, score, regime
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def close(self) -> None:
        self.connection.close()
