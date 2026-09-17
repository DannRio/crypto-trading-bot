import pytest

from risk_manager import create_trade_plan
from strategy import Signal


def test_long_trade_plan():
    plan = create_trade_plan(
        signal=Signal.LONG,
        entry_price=1100,
        atr=24,
        account_equity=10000,
    )

    assert plan is not None
    assert plan.risk_amount == pytest.approx(100)
    assert plan.stop_distance == pytest.approx(48)
    assert plan.stop_loss == pytest.approx(1052)
    assert plan.take_profit == pytest.approx(1196)


def test_short_trade_plan():
    plan = create_trade_plan(
        signal=Signal.SHORT,
        entry_price=1100,
        atr=24,
        account_equity=10000,
    )

    assert plan is not None
    assert plan.stop_loss == pytest.approx(1148)
    assert plan.take_profit == pytest.approx(1004)


def test_no_trade_returns_none():
    plan = create_trade_plan(
        signal=Signal.NO_TRADE,
        entry_price=1100,
        atr=24,
        account_equity=10000,
    )

    assert plan is None