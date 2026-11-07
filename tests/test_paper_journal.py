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
    assert len(trades) == 1
    assert trades.iloc[0]["net_pnl"] == 20.0

    journal.close()
