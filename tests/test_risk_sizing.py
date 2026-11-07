import pytest

from trading_bot.risk import size_position_for_stop


def test_notional_cap_reduces_actual_risk():
    result = size_position_for_stop(
        entry_price=10_000,
        stop_loss=9_900,
        account_equity=1_000,
        risk_percent=0.01,
        max_notional_percent=1.0,
    )
    assert result.notional_value == pytest.approx(1_000)
    assert result.position_size == pytest.approx(0.1)
    assert result.target_risk_amount == pytest.approx(10)
    assert result.actual_risk_amount == pytest.approx(10)
