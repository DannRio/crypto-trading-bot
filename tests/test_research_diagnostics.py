"""Diagnostic accounting invariants and exit ablation smoke coverage."""
import pandas as pd
import pytest

from trading_bot.backtesting.models import CompletedTrade
from trading_bot.research.diagnostics import trade_diagnostics
from trading_bot.research.exit_ablation import EXIT_VARIANTS


def test_attribution_conserves_pnl_and_r():
    trades = pd.DataFrame([
        CompletedTrade(
            signal="LONG",
            signal_time=pd.Timestamp("2026-01-01", tz="UTC"),
            entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
            exit_time=pd.Timestamp("2026-01-02", tz="UTC"),
            entry_price=100, exit_price=104,
            stop_loss=95, take_profit=110,
            position_size=2,
            pnl=6,
            exit_reason="TAKE_PROFIT_2",
            strategy="trend_continuation_v2",
            score=82,
            regime="BULL_TREND",
            risk_amount=10,
            r_multiple=0.6,
            gross_pnl=7,
            entry_fee=0.5,
            exit_fees=0.5,
            gross_r=0.7,
            fees_r=0.1,
            raw_gross_pnl=8,
            slippage_cost=1,
            slippage_r=0.1,
        )
    ])
    output = trade_diagnostics(trades, 10000)
    assert output["gross_total"] - output["slippage_total"] - output["fees_total"] == pytest.approx(trades["pnl"].sum())
    assert output["raw_average_r"] - output["slippage_average_r"] - output["fees_average_r"] == pytest.approx(trades["r_multiple"].mean())
    assert output["by_strategy"][0]["strategy"] == "trend_continuation_v2"
    assert output["by_year"][0]["year"] == "2026"


def test_exit_ablation_keeps_fixed_named_models():
    assert set(EXIT_VARIANTS) == {"full_2r", "partial_no_be", "partial_be"}
    assert EXIT_VARIANTS["full_2r"]["take_profit_1_fraction"] == 0
