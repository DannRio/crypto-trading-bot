from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.regime_bot import RegimeBacktester


def make_repeated_breakouts(cycles: int = 5) -> pd.DataFrame:
    closes: list[float] = []
    volumes: list[float] = []
    price = 100.0

    for index in range(225):
        price += 0.05 + 0.02 * np.sin(index / 5.0)
        closes.append(price)
        volumes.append(1_000.0)

    for _ in range(cycles):
        center = price
        for index in range(35):
            price = center + 0.10 * np.sin(index / 3.0)
            closes.append(price)
            volumes.append(1_000.0)
        price = center + 0.34
        closes.append(price)
        volumes.append(2_600.0)
        for index in range(25):
            price += 0.08 + 0.01 * np.sin(index)
            closes.append(price)
            volumes.append(1_050.0)

    close = np.asarray(closes)
    open_price = np.r_[close[0] - 0.04, close[:-1]]
    high = np.maximum(open_price, close) + 0.10
    low = np.minimum(open_price, close) - 0.10
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2024-01-01",
                periods=len(close),
                freq="15min",
                tz="UTC",
            ),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volumes,
        }
    )


def test_backtest_uses_next_bar_execution_and_includes_costs() -> None:
    result = RegimeBacktester().run(
        symbol="TEST/USDT",
        frames={"15m": make_repeated_breakouts()},
        execution_timeframe="15m",
        starting_equity=10_000.0,
    )

    assert result.trades == 5
    assert result.ending_equity == pytest.approx(
        result.starting_equity + sum(trade.net_pnl for trade in result.trades_detail)
    )
    assert all(trade.entry_time >= trade.signal_time for trade in result.trades_detail)
    assert all(trade.fees > 0 for trade in result.trades_detail)
    assert result.max_drawdown_percent >= 0
    assert result.equity_curve


def test_backtest_rejects_too_little_history() -> None:
    with pytest.raises(ValueError, match="at least"):
        RegimeBacktester().run(
            symbol="TEST/USDT",
            frames={"15m": make_repeated_breakouts().iloc[:100]},
            execution_timeframe="15m",
        )


def test_ambiguous_bar_uses_stop_first_assumption() -> None:
    from datetime import datetime, timezone

    from trading_bot.regime_bot.backtest import _Position
    from trading_bot.regime_bot.models import PositionSide, SetupType

    backtester = RegimeBacktester()
    position = _Position(
        side=PositionSide.LONG,
        setup_type=SetupType.BREAKOUT,
        signal_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        entry_time=datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc),
        entry_price=100.0,
        initial_stop=99.0,
        stop=99.0,
        target1=101.0,
        target2=102.0,
        target3=103.0,
        original_quantity=1.0,
        remaining_quantity=1.0,
        initial_risk=1.3,
        opening_fee=0.1,
        accumulated_fees=0.1,
        highest_high=100.0,
        lowest_low=100.0,
        bars_held=1,
    )
    row = pd.Series(
        {
            "open": 100.0,
            "high": 104.0,
            "low": 98.0,
            "close": 102.0,
            "atr": 1.0,
        }
    )
    remaining, _, trade = backtester._manage_position(
        position=position,
        row=row,
        timestamp=datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc),
        equity=10_000.0,
        fee_rate=0.001,
        slippage_rate=0.0005,
    )
    assert remaining is None
    assert trade is not None
    assert trade.exit_reason == "stop_loss"
    assert trade.net_pnl < 0


def test_gap_through_stop_uses_adverse_open_fill() -> None:
    from datetime import datetime, timezone

    from trading_bot.regime_bot.backtest import _Position
    from trading_bot.regime_bot.models import PositionSide, SetupType

    backtester = RegimeBacktester()
    position = _Position(
        side=PositionSide.LONG,
        setup_type=SetupType.BREAKOUT,
        signal_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        entry_time=datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc),
        entry_price=100.0,
        initial_stop=99.0,
        stop=99.0,
        target1=101.0,
        target2=102.0,
        target3=103.0,
        original_quantity=1.0,
        remaining_quantity=1.0,
        initial_risk=1.3,
        opening_fee=0.1,
        accumulated_fees=0.1,
        highest_high=100.0,
        lowest_low=100.0,
        bars_held=1,
    )
    row = pd.Series(
        {
            "open": 97.0,
            "high": 98.0,
            "low": 96.0,
            "close": 97.5,
            "atr": 1.0,
        }
    )
    remaining, _, trade = backtester._manage_position(
        position=position,
        row=row,
        timestamp=datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc),
        equity=10_000.0,
        fee_rate=0.001,
        slippage_rate=0.0005,
    )
    assert remaining is None
    assert trade is not None
    assert trade.exit_price < 97.0
    assert trade.exit_price < position.initial_stop
