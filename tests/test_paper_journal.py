import sqlite3

import pandas as pd

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.paper import PaperBroker, PaperJournal
from trading_bot.planning import TradeSetup


def setup() -> TradeSetup:
    return TradeSetup(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=80,
        reference_entry=100.0,
        entry_low=99.0,
        entry_high=101.0,
        stop_loss=95.0,
        take_profit_1=105.0,
        take_profit_2=110.0,
        reward_to_risk_1=1.0,
        reward_to_risk_2=2.0,
        position_size=2.0,
        notional_value=200.0,
        target_risk_amount=10.0,
        actual_risk_amount=10.0,
        reasons=("test",),
    )


def test_journal_persists_closed_trade(tmp_path):
    journal = PaperJournal(tmp_path / "paper.db")
    broker = PaperBroker(
        starting_equity=10_000,
        fee_rate=0.0,
        slippage_bps=0.0,
        journal=journal,
    )

    broker.submit_setup(
        "BTC/USDT",
        "1h",
        setup(),
        "2026-01-01 00:00:00+00:00",
    )

    broker.process_candle(
        "BTC/USDT",
        "1h",
        {
            "timestamp": pd.Timestamp(
                "2026-01-01 01:00:00",
                tz="UTC",
            ),
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
    )
    broker.process_candle(
        "BTC/USDT",
        "1h",
        {
            "timestamp": pd.Timestamp(
                "2026-01-01 02:00:00",
                tz="UTC",
            ),
            "high": 111.0,
            "low": 100.0,
            "close": 110.0,
        },
    )

    orders = journal.orders()
    trades = journal.trades()

    assert len(orders) == 1
    assert orders.iloc[0]["status"] == "CLOSED"
    assert orders.iloc[0]["strategy"] == "trend_continuation"
    assert len(trades) == 1
    assert trades.iloc[0]["net_pnl"] == 20.0
    assert trades.iloc[0]["strategy"] == "trend_continuation"

    journal.close()



def test_journal_migrates_legacy_tables_with_strategy_column(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_orders (
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

        CREATE TABLE paper_trades (
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
    connection.commit()
    connection.close()

    journal = PaperJournal(path)

    order_columns = {
        row[1]
        for row in journal.connection.execute(
            "PRAGMA table_info(paper_orders)"
        ).fetchall()
    }
    trade_columns = {
        row[1]
        for row in journal.connection.execute(
            "PRAGMA table_info(paper_trades)"
        ).fetchall()
    }

    assert "strategy" in order_columns
    assert "strategy" in trade_columns
    journal.close()
