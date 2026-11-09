import pandas as pd
import pytest

from trading_bot.core.enums import Signal
from trading_bot.paper import PaperJournal
from trading_bot.paper.models import PaperOrder, PaperOrderStatus, PaperPosition
from trading_bot.paper.risk import (
    PortfolioRiskConfig,
    PortfolioRiskManager,
)


def position(
    symbol="BTC/USDT",
    signal=Signal.LONG,
    risk=80.0,
    notional=5_000.0,
):
    entry = 100.0
    size = notional / entry
    return PaperPosition(
        order_id=f"pos-{symbol}",
        symbol=symbol,
        timeframe="1h",
        signal=signal,
        entry_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        entry_price=entry,
        stop_loss=98.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        position_size=size,
        risk_amount=risk,
        entry_fee=0.0,
        score=80,
        regime="BULL_TREND",
        strategy="trend_continuation",
    )


def pending(
    symbol="ETH/USDT",
    signal=Signal.LONG,
    risk=80.0,
    notional=5_000.0,
):
    entry = 100.0
    size = notional / entry
    return PaperOrder(
        order_id=f"ord-{symbol}",
        symbol=symbol,
        timeframe="1h",
        signal=signal,
        created_at=pd.Timestamp("2026-01-01T00:00:00Z"),
        reference_entry=entry,
        stop_loss=98.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        position_size=size,
        planned_risk=risk,
        score=80,
        regime="BULL_TREND",
        strategy="trend_continuation",
        bars_remaining=3,
        status=PaperOrderStatus.PENDING,
    )


def permissive_config(**overrides):
    values = {
        "max_open_risk_percent": 0.50,
        "max_positions": 10,
        "max_pending_orders": 10,
        "max_symbol_risk_percent": 0.50,
        "max_gross_notional_percent": 10.0,
        "max_correlated_risk_percent": 0.50,
        "correlation_threshold": 0.75,
        "require_correlation_data": True,
        "max_daily_loss_percent": 0.50,
        "max_weekly_loss_percent": 0.50,
        "max_drawdown_percent": 0.50,
    }
    values.update(overrides)
    return PortfolioRiskConfig(**values)


def test_daily_loss_breach_creates_persistent_halt():
    manager = PortfolioRiskManager(
        permissive_config(
            max_daily_loss_percent=0.02,
        )
    )
    start = manager.initialize_state(
        10_000,
        "2026-01-05T00:00:00Z",
    )

    update = manager.update_state(
        start,
        9_790,
        "2026-01-05T12:00:00Z",
    )

    assert update.triggered is True
    assert update.state.halted is True
    assert update.daily_loss_pct == pytest.approx(0.021)
    assert "Daily loss limit" in update.reason

    later = manager.update_state(
        update.state,
        10_100,
        "2026-01-06T12:00:00Z",
    )
    assert later.state.halted is True
    assert later.triggered is False


def test_drawdown_tracks_peak_equity():
    manager = PortfolioRiskManager(
        permissive_config(
            max_drawdown_percent=0.10,
        )
    )
    state = manager.initialize_state(
        10_000,
        "2026-01-05T00:00:00Z",
    )
    state = manager.update_state(
        state,
        12_000,
        "2026-01-05T01:00:00Z",
    ).state

    update = manager.update_state(
        state,
        10_700,
        "2026-01-05T02:00:00Z",
    )

    assert update.drawdown_pct == pytest.approx(
        (12_000 - 10_700) / 12_000
    )
    assert update.state.halted is True
    assert "drawdown" in update.reason.lower()


def test_gross_notional_limit_blocks_excess_exposure():
    manager = PortfolioRiskManager(
        permissive_config(
            max_gross_notional_percent=1.0,
        )
    )

    check = manager.check_new_order(
        account_equity=10_000,
        symbol="ETH/USDT",
        signal=Signal.LONG,
        new_risk=50.0,
        new_notional=4_000.0,
        positions=[
            position(
                risk=50.0,
                notional=7_000.0,
            )
        ],
        pending_orders=[],
        correlations={"BTC/USDT": 0.0},
    )

    assert check.allowed is False
    assert check.code == "MAX_GROSS_NOTIONAL"


def test_same_direction_positive_correlation_counts_as_concentrated():
    manager = PortfolioRiskManager(
        permissive_config(
            max_correlated_risk_percent=0.015,
        )
    )

    check = manager.check_new_order(
        account_equity=10_000,
        symbol="ETH/USDT",
        signal=Signal.LONG,
        new_risk=80.0,
        new_notional=1_000.0,
        positions=[position(risk=80.0, notional=1_000.0)],
        pending_orders=[],
        correlations={"BTC/USDT": 0.90},
    )

    assert check.allowed is False
    assert check.code == "MAX_CORRELATED_RISK"


def test_positive_correlation_opposite_direction_is_not_same_risk_bucket():
    manager = PortfolioRiskManager(
        permissive_config(
            max_correlated_risk_percent=0.015,
        )
    )

    check = manager.check_new_order(
        account_equity=10_000,
        symbol="ETH/USDT",
        signal=Signal.SHORT,
        new_risk=80.0,
        new_notional=1_000.0,
        positions=[position(risk=80.0, notional=1_000.0)],
        pending_orders=[],
        correlations={"BTC/USDT": 0.90},
    )

    assert check.allowed is True


def test_negative_correlation_opposite_direction_counts_as_concentrated():
    manager = PortfolioRiskManager(
        permissive_config(
            max_correlated_risk_percent=0.015,
        )
    )

    check = manager.check_new_order(
        account_equity=10_000,
        symbol="ETH/USDT",
        signal=Signal.SHORT,
        new_risk=80.0,
        new_notional=1_000.0,
        positions=[position(risk=80.0, notional=1_000.0)],
        pending_orders=[],
        correlations={"BTC/USDT": -0.90},
    )

    assert check.allowed is False
    assert check.code == "MAX_CORRELATED_RISK"


def test_missing_correlation_data_fails_closed():
    manager = PortfolioRiskManager(
        permissive_config(
            require_correlation_data=True,
        )
    )

    check = manager.check_new_order(
        account_equity=10_000,
        symbol="ETH/USDT",
        signal=Signal.LONG,
        new_risk=50.0,
        new_notional=1_000.0,
        positions=[position(risk=50.0, notional=1_000.0)],
        pending_orders=[],
        correlations={},
    )

    assert check.allowed is False
    assert check.code == "CORRELATION_DATA_MISSING"


def test_risk_state_round_trips_through_journal(tmp_path):
    manager = PortfolioRiskManager()
    state = manager.initialize_state(
        10_000,
        "2026-01-05T00:00:00Z",
    )
    state = manager.update_state(
        state,
        9_700,
        "2026-01-05T12:00:00Z",
    ).state

    journal = PaperJournal(tmp_path / "paper.db")
    journal.save_risk_state(state)
    restored = journal.load_risk_state()

    assert restored is not None
    assert restored.halted is True
    assert restored.last_equity == pytest.approx(9_700)
    assert restored.evaluated_at == pd.Timestamp(
        "2026-01-05T12:00:00Z"
    )
    assert restored.halt_reason == state.halt_reason
    journal.close()
