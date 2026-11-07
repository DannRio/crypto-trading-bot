import pandas as pd

from trading_bot.analysis import add_indicators
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.decision import DecisionEngine


def trending_data(rows=260):
    prices = [100 + i * 0.5 for i in range(rows)]
    volumes = [1000.0] * rows
    volumes[-1] = 1600.0
    return pd.DataFrame(
        {
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": volumes,
        }
    )


def test_decision_engine_produces_confirmed_setup():
    data = add_indicators(trending_data())

    decision = DecisionEngine(
        account_equity=10_000,
        risk_percent=0.01,
        min_confirmed_score=50,
    ).evaluate(data)

    assert decision.assessment.signal == Signal.LONG
    assert decision.assessment.status == SetupStatus.CONFIRMED
    assert decision.setup is not None
    assert decision.setup.stop_loss < decision.setup.reference_entry
