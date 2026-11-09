import pytest

from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.planning import TradePlanConfig, create_trade_setup


def structure() -> MarketStructure:
    return MarketStructure(
        regime=MarketRegime.BULL_TREND,
        support=970.0,
        resistance=995.0,
        atr_pct=0.01,
        volume_ratio=1.2,
        higher_high=True,
        higher_low=True,
        lower_high=False,
        lower_low=False,
        high_volatility=False,
    )


def test_confirmed_long_setup_has_risk_defined_prices():
    setup = create_trade_setup(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=90,
        reasons=("test",),
        entry_price=1000.0,
        atr=10.0,
        structure=structure(),
        account_equity=10_000,
    )
    assert setup is not None
    assert setup.stop_loss == pytest.approx(980.0)
    assert setup.take_profit_1 == pytest.approx(1020.0)
    assert setup.take_profit_2 == pytest.approx(1040.0)
    assert setup.position_size == pytest.approx(5.0)
    assert setup.actual_risk_amount == pytest.approx(100.0)


def test_forming_setup_has_no_trade_plan():
    setup = create_trade_setup(
        signal=Signal.LONG,
        status=SetupStatus.FORMING,
        score=60,
        reasons=("test",),
        entry_price=1000.0,
        atr=10.0,
        structure=structure(),
        account_equity=10_000,
    )
    assert setup is None



def test_trade_plan_config_changes_stop_and_targets():
    setup = create_trade_setup(
        signal=Signal.LONG,
        status=SetupStatus.CONFIRMED,
        score=90,
        reasons=("test",),
        entry_price=1000.0,
        atr=10.0,
        structure=structure(),
        account_equity=10_000,
        config=TradePlanConfig(
            atr_stop_multiplier=1.5,
            reward_to_risk_1=1.5,
            reward_to_risk_2=3.0,
        ),
    )

    assert setup is not None
    assert setup.stop_loss == pytest.approx(982.5)
    risk_per_unit = setup.reference_entry - setup.stop_loss
    assert setup.take_profit_1 == pytest.approx(
        1000.0 + 1.5 * risk_per_unit
    )
    assert setup.take_profit_2 == pytest.approx(
        1000.0 + 3.0 * risk_per_unit
    )
    assert setup.reward_to_risk_2 == 3.0
