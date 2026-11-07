import pandas as pd
import pytest

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.paper import (
    PaperBroker,
    PortfolioRiskConfig,
)
from trading_bot.planning import TradeSetup


def long_setup(risk=10.0) -> TradeSetup:
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
        target_risk_amount=risk,
        actual_risk_amount=risk,
        reasons=("test",),
    )


def candle(timestamp, high, low, close):
    return {
        "timestamp": pd.Timestamp(timestamp, tz="UTC"),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000.0,
    }


def test_paper_order_fills_and_hits_tp2():
    broker = PaperBroker(
        starting_equity=10_000,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    broker.submit_setup(
        "BTC/USDT",
        "1h",
        long_setup(),
        "2026-01-01 00:00:00+00:00",
        regime="BULL_TREND",
    )

    broker.process_candle(
        "BTC/USDT",
        "1h",
        candle(
            "2026-01-01 01:00:00",
            high=101.0,
            low=99.0,
            close=100.0,
        ),
    )
    assert len(broker.positions) == 1

    trades = broker.process_candle(
        "BTC/USDT",
        "1h",
        candle(
            "2026-01-01 02:00:00",
            high=111.0,
            low=100.0,
            close=110.0,
        ),
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "TAKE_PROFIT_2"
    assert trades[0].net_pnl == pytest.approx(20.0)
    assert broker.snapshot().equity == pytest.approx(10_020.0)


def test_duplicate_exposure_is_rejected():
    broker = PaperBroker()
    setup = long_setup()

    broker.submit_setup(
        "BTC/USDT",
        "1h",
        setup,
        "2026-01-01 00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="Exposure already exists"):
        broker.submit_setup(
            "BTC/USDT",
            "1h",
            setup,
            "2026-01-01 00:01:00+00:00",
        )


def test_portfolio_risk_limit_rejects_large_setup():
    broker = PaperBroker(
        starting_equity=10_000,
        risk_config=PortfolioRiskConfig(
            max_open_risk_percent=0.005,
            max_symbol_risk_percent=0.005,
        ),
    )

    with pytest.raises(ValueError, match="risk limit"):
        broker.submit_setup(
            "BTC/USDT",
            "1h",
            long_setup(risk=100.0),
            "2026-01-01 00:00:00+00:00",
        )
