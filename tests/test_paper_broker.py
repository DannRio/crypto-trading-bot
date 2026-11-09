import pandas as pd
import pytest

from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.paper import (
    PaperBroker,
    PortfolioRiskConfig,
)
from trading_bot.planning import TradeSetup


def long_setup(
    risk=10.0,
    tp1_fraction=0.5,
    move_stop_to_break_even=True,
) -> TradeSetup:
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
        take_profit_1_fraction=tp1_fraction,
        move_stop_to_break_even_after_tp1=(
            move_stop_to_break_even
        ),
    )


def candle(timestamp, high, low, close, open_=None):
    return {
        "timestamp": pd.Timestamp(timestamp, tz="UTC"),
        "open": close if open_ is None else open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000.0,
    }


def test_paper_order_fills_and_executes_tp1_then_tp2():
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
            low=101.0,
            close=110.0,
        ),
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "TAKE_PROFIT_2_AFTER_TP1"
    assert trades[0].tp1_hit is True
    assert trades[0].net_pnl == pytest.approx(15.0)
    assert broker.snapshot().equity == pytest.approx(10_015.0)


def test_entry_zone_fills_without_exact_reference_touch():
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
    )

    broker.process_candle(
        "BTC/USDT",
        "1h",
        candle(
            "2026-01-01 01:00:00",
            high=99.8,
            low=99.2,
            close=99.5,
            open_=99.5,
        ),
    )

    position = broker.positions[("BTC/USDT", "1h")]
    assert position.entry_price == pytest.approx(99.5)


def test_tp1_partial_exit_moves_remaining_stop_to_break_even():
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

    first = broker.process_candle(
        "BTC/USDT",
        "1h",
        candle(
            "2026-01-01 02:00:00",
            high=106.0,
            low=101.0,
            close=105.0,
        ),
    )
    assert first == []
    position = broker.positions[("BTC/USDT", "1h")]
    assert position.tp1_hit is True
    assert position.position_size == pytest.approx(1.0)
    assert position.effective_stop == pytest.approx(100.0)
    assert position.risk_amount == pytest.approx(0.0)

    trades = broker.process_candle(
        "BTC/USDT",
        "1h",
        candle(
            "2026-01-01 03:00:00",
            high=104.0,
            low=99.0,
            close=100.0,
        ),
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == "STOP_LOSS_AFTER_TP1"
    assert trades[0].net_pnl == pytest.approx(5.0)


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
