"""Regression coverage for portfolio equity, yearly returns and descriptive buckets."""
import math

import numpy as np
import pandas as pd
import pytest

from trading_bot.backtesting.models import BacktestResult
from trading_bot.backtesting import run_decision_backtest
from trading_bot.research.diagnostics import build_diagnostics, score_bucket


def example_result():
    curve = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2023-01-01T00:00Z", "2023-12-31T23:00Z",
            "2024-01-01T00:00Z", "2024-12-31T23:00Z",
            "2025-01-01T00:00Z", "2025-12-31T23:00Z",
        ], utc=True),
        "equity": [10000, 10300, 10290, 9900, 9850, 9400],
        "realized_equity": [10000, 10300, 10300, 9900, 9900, 9400],
    })
    trades = pd.DataFrame([
        dict(signal="LONG", signal_time="2023-10-01T00:00Z",
             entry_time="2023-10-01T01:00Z", exit_time="2023-12-31T01:00Z",
             entry_price=100,exit_price=103,stop_loss=96,take_profit=110,
             position_size=100,pnl=300,exit_reason="TAKE_PROFIT_2",
             strategy="trend_continuation",score=75,regime="BULL_TREND",
             risk_amount=100,r_multiple=3),
        dict(signal="LONG", signal_time="2024-01-02T00:00Z",
             entry_time="2024-01-02T01:00Z", exit_time="2024-12-31T02:00Z",
             entry_price=110,exit_price=105,stop_loss=104,take_profit=125,
             position_size=80,pnl=-400,exit_reason="STOP_LOSS",
             strategy="trend_continuation",score=83,regime="RANGE",
             risk_amount=200,r_multiple=-2),
        dict(signal="LONG", signal_time="2025-06-01T00:00Z",
             entry_time="2025-06-01T01:00Z", exit_time="2025-12-31T03:00Z",
             entry_price=110,exit_price=105,stop_loss=104,take_profit=125,
             position_size=100,pnl=-500,exit_reason="STOP_LOSS",
             strategy="trend_continuation",score=93,regime="BULL_TREND",
             risk_amount=250,r_multiple=-2),
    ])
    return BacktestResult(
        initial_equity=10000,final_equity=9400,total_return_pct=-6,
        max_drawdown_pct=(10300-9400)/10300*100,
        total_trades=3,win_rate_pct=100/3,
        profit_factor=300/900,average_pnl=-200,
        trades=trades,equity_curve=curve,
    )


def test_annual_returns_use_continuous_marked_equity_not_independent_reset():
    report=build_diagnostics(example_result())
    years=report["yearly"]
    assert [r["year"] for r in years]==[2023,2024,2025]
    assert years[0]["return_pct"]==pytest.approx(3)
    assert years[1]["return_pct"]==pytest.approx((9900/10300-1)*100)
    assert years[2]["return_pct"]==pytest.approx((9400/9900-1)*100)
    assert years[0]["start_equity"]==10000
    assert years[2]["end_equity"]==9400
    assert math.prod(1+y["return_pct"]/100 for y in years)==pytest.approx(.94)
    assert sum(y["trades_closed"] for y in years)==3
    assert sum(y["closed_trade_pnl"] for y in years)==pytest.approx(-600)


def test_score_buckets_and_entry_regime_are_descriptive_and_complete():
    report=build_diagnostics(example_result())
    assert [b["label"] for b in report["score_buckets"]]==[
        "0–69","70–79","80–89","90–100"
    ]
    assert [b["trades"] for b in report["score_buckets"]]==[0,1,1,1]
    assert sum(b["net_pnl"] for b in report["score_buckets"])==-600
    assert {r["label"]:r["trades"] for r in report["regimes"]}=={
        "BULL_TREND":2,"RANGE":1,
    }
    assert report["summary"]["profit_factor"]==pytest.approx(1/3)
    assert report["trades"][0]["score_bucket"]=="70–79"
    assert report["trades"][1]["r_multiple"]==-2
    assert score_bucket(69)=="0–69"
    assert score_bucket(70)=="70–79"
    assert score_bucket(89)=="80–89"
    assert score_bucket(100)=="90–100"


def test_drawdown_curve_preserves_extrema_when_downsampling():
    original=example_result()
    base=original.equity_curve.iloc[[0]].copy()
    timestamps=pd.date_range("2023-01-01",periods=2400,freq="h",tz="UTC")
    values=np.linspace(10000,9400,len(timestamps))
    values[1300]=6000  # dramatic transient loss must survive downsampling
    original.equity_curve=pd.DataFrame({"timestamp":timestamps,"equity":values})
    report=build_diagnostics(original,max_equity_points=120)
    assert len(report["equity_curve"])<=120
    assert min(point["equity"] for point in report["equity_curve"])==pytest.approx(6000)
    assert report["equity_curve"][0]["time"].startswith("2023-01-01")
    assert report["equity_curve"][-1]["equity"]==9400


def test_empty_trades_return_null_win_rate_not_false_success():
    result=example_result()
    result.trades=pd.DataFrame()
    result.final_equity=10000
    result.total_return_pct=0
    result.equity_curve["equity"]=10000
    report=build_diagnostics(result)
    assert report["summary"]["trades"]==0
    assert report["summary"]["win_rate_pct"] is None
    assert report["summary"]["profit_factor"] is None
    assert all(y["trades_closed"]==0 for y in report["yearly"])


def test_mismatched_final_equity_refused():
    result=example_result()
    result.equity_curve.loc[result.equity_curve.index[-1],"equity"]=12345
    with pytest.raises(ValueError,match="Final marked equity"):
        build_diagnostics(result)


def test_shared_backtester_emits_real_equity_samples():
    n=340
    prices=np.linspace(100,140,n)
    data=pd.DataFrame({
        "timestamp":pd.date_range("2023-01-01",periods=n,freq="h",tz="UTC"),
        "open":prices,
        "high":prices+1.5,
        "low":prices-1.5,
        "close":prices,
        "volume":np.full(n,1200.0),
    })
    result=run_decision_backtest(data,allow_short=False,min_confirmed_score=50)
    assert not result.equity_curve.empty
    assert result.equity_curve["timestamp"].is_monotonic_increasing
    assert result.equity_curve.iloc[-1]["equity"]==pytest.approx(result.final_equity)
    report=build_diagnostics(result)
    assert report["summary"]["trades"]==result.total_trades
