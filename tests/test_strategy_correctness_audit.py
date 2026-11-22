"""Regression cases for the strategy audit. Synthetic correctness, not an edge test."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_bot.analysis import add_indicators
from trading_bot.analysis.market_structure import MarketStructure
from trading_bot.backtesting import run_decision_backtest
from trading_bot.backtesting.timeframe_gates import HistoricalTimeframeGates
from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.data.validation import validate_ohlcv
from trading_bot.decision import DecisionEngine
from trading_bot.decision.models import DecisionResult
from trading_bot.planning import TradePlanConfig, TradeSetup, create_trade_setup
from trading_bot.scanner import MarketScanner
from trading_bot.scanner.multi_timeframe import regime_alignment, apply_confirmation_gate, TimeframeConfirmation
from trading_bot.strategies import SetupAssessment
from trading_bot.strategies.breakout_retest import evaluate_breakout_retest
from trading_bot.strategies.range_mean_reversion import evaluate_range_mean_reversion
from trading_bot.strategies.trend_continuation_v2 import evaluate_trend_continuation_v2


def frame(rows=260, freq="1h"):
    close = np.arange(rows) * .1 + 100
    return pd.DataFrame(dict(timestamp=pd.date_range("2025-01-01", periods=rows, freq=freq, tz="UTC"),
        open=close, high=close+1, low=close-1, close=close, volume=np.full(rows, 1000.)))


def structure(regime=MarketRegime.BULL_TREND):
    return MarketStructure(regime, 100., 120., .02, 1.5, True, True, False, False, False)


def assessment(strategy="trend_continuation", score=80):
    return SetupAssessment(Signal.LONG, SetupStatus.CONFIRMED, score, "test", ("test",), strategy)


def setup(event_id=None):
    return TradeSetup(Signal.LONG, SetupStatus.CONFIRMED, 80, 100., 99., 101., 95., 105., 110.,
        1., 2., 2., 200., 10., 10., ("test",), event_id=event_id, cancel_price=95.,
        setup_type="trend_pullback" if event_id else "legacy_state")


def decision(event_id=None):
    a = replace(assessment(), event_id=event_id, setup_type="trend_pullback" if event_id else "legacy_state")
    return SimpleNamespace(assessment=a, setup=setup(event_id), structure=structure())


def price_frame(rows):
    return pd.DataFrame([(t, *r, 1000.) for t, r in zip(pd.date_range("2025-01-01", periods=len(rows), freq="h", tz="UTC"), rows)],
                        columns=["timestamp", "open", "high", "low", "close", "volume"])


def fake_engine(monkeypatch, evaluator):
    import trading_bot.backtesting.decision_engine as module
    class Fake:
        def __init__(self, **kwargs): pass
        def evaluate(self, history): return evaluator(history)
    monkeypatch.setattr(module, "DecisionEngine", Fake)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf, "not-a-number"])
@pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
def test_reject_nonfinite_or_non_numeric(bad, column):
    data = frame(3).astype({column: object})
    data.loc[1, column] = bad
    report = validate_ohlcv(data, "1h")
    assert not report.valid
    assert any("non-finite" in issue for issue in report.issues)


@pytest.mark.parametrize("bad", [pd.NaT, "not-a-date"])
def test_reject_bad_timestamps(bad):
    data = frame(3)
    data["timestamp"] = data["timestamp"].astype(object)
    data.loc[1, "timestamp"] = bad
    assert not validate_ohlcv(data, "1h").valid


def test_reject_misaligned_and_unsorted_timestamps():
    data = frame(3)
    data.loc[1, "timestamp"] += pd.Timedelta(minutes=1)
    assert not validate_ohlcv(data, "1h").valid
    assert not validate_ohlcv(frame(3).iloc[::-1], "1h").valid


def test_primary_gap_fails_before_trading():
    with pytest.raises(ValueError, match="missing candle"):
        run_decision_backtest(frame().drop(index=240))


def test_stale_higher_timeframe_does_not_confirm():
    gates = HistoricalTimeframeGates("1h", {"4h": frame(240, "4h")}, ("4h",))
    result = gates.apply(DecisionResult(assessment(), None, object()), pd.Timestamp("2025-04-01T00:00:00Z"))
    assert result.assessment.status == SetupStatus.FORMING
    assert result.setup is None


def test_scanner_rejects_stale_primary_data():
    scanner = MarketScanner(data_fetcher=lambda **kw: frame(), now=lambda: pd.Timestamp("2025-04-01T00:00:00Z"))
    result = scanner.scan_symbol("BTC/USDT")
    assert result.setup is None
    assert "Stale" in result.error


def test_range_context_is_not_forced_to_be_trending():
    assert regime_alignment(Signal.LONG, MarketRegime.RANGE, "range_mean_reversion") is True
    assert regime_alignment(Signal.SHORT, MarketRegime.RANGE, "range_mean_reversion") is True
    assert regime_alignment(Signal.LONG, MarketRegime.BEAR_TREND, "range_mean_reversion") is False
    assert regime_alignment(Signal.LONG, MarketRegime.RANGE) is None


def test_metadata_survives_timeframe_gates():
    original = replace(assessment(), event_id="event", anchor_price=99, invalidation_price=95)
    gated, _ = apply_confirmation_gate(original, (TimeframeConfirmation("4h", MarketRegime.BULL_TREND, True, None),))
    assert gated.event_id == "event" and gated.invalidation_price == 95


def breakout_fixture():
    data = frame(30)
    data[["open", "high", "low", "close"]] = [104.5, 110., 100., 105.]
    data["rsi_14"], data["atr_14"], data["volume_sma_20"] = 60., 2., 1000.
    data.loc[25:28, ["open", "high", "low", "close", "volume"]] = [111.5, 113., 109., 112., 1500.]
    data.loc[29, ["open", "high", "low", "close", "volume"]] = [110.5, 112., 109.8, 111., 1200.]
    return data


@pytest.mark.parametrize("bad", ["failed_hold", "no_touch", "bearish_retest"])
def test_breakout_cannot_score_around_failed_retest(bad):
    data = breakout_fixture()
    if bad == "failed_hold": data.loc[29, ["open", "low", "close"]] = [110., 108., 109.]
    if bad == "no_touch": data.loc[29, ["open", "high", "low", "close"]] = [112., 114., 111.5, 113.]
    if bad == "bearish_retest": data.loc[29, "open"] = 111.5
    assert evaluate_breakout_retest(data, structure()).status != SetupStatus.CONFIRMED


def test_repeated_retests_share_frozen_event():
    data = breakout_fixture()
    first = evaluate_breakout_retest(data.iloc[:-1], structure())
    second = evaluate_breakout_retest(data, structure())
    assert first.status == second.status == SetupStatus.CONFIRMED
    assert first.event_id == second.event_id
    assert second.anchor_price == 110


@pytest.mark.parametrize("bad", ["rsi", "extension"])
def test_v2_breakout_cannot_bypass_momentum_or_chase_limit(bad):
    data = pd.DataFrame([
        dict(open=108, high=110, low=107, close=109, ema_50=105, ema_200=95, rsi_14=49, atr_14=2),
        dict(open=109, high=110, low=108, close=109, ema_50=105, ema_200=95, rsi_14=50, atr_14=2),
        dict(open=109, high=112, low=108, close=111, ema_50=105, ema_200=95, rsi_14=55, atr_14=2),
    ])
    st = replace(structure(), resistance=110)
    assert evaluate_trend_continuation_v2(data, st).status == SetupStatus.CONFIRMED
    data.loc[2, "rsi_14" if bad == "rsi" else "ema_50"] = 95 if bad == "rsi" else 100
    assert evaluate_trend_continuation_v2(data, st).status != SetupStatus.CONFIRMED


def test_range_oversold_without_rejection_is_not_entry():
    data = pd.DataFrame([dict(open=103, high=104, low=100.2, close=102, rsi_14=25, atr_14=2)])
    assert evaluate_range_mean_reversion(data, structure(MarketRegime.RANGE)).status == SetupStatus.NO_TRADE


def test_range_planner_keeps_target_inside_boundary():
    plan = create_trade_setup(Signal.LONG, SetupStatus.CONFIRMED, 80, ("test",), 102., 2.,
        structure(MarketRegime.RANGE), 10000., event_id="range", setup_type="range_reversal",
        anchor_price=100., invalidation_price=99.5, target_price=119.5)
    assert plan is not None
    assert plan.stop_loss == 99.5
    assert 102 < plan.take_profit_1 < plan.take_profit_2 < 120


def test_distant_structural_stop_is_rejected_not_tightened():
    assert create_trade_setup(Signal.LONG, SetupStatus.CONFIRMED, 80, ("test",), 110., 2.,
        structure(), 10000., event_id="far", invalidation_price=90.) is None


def test_candidate_gates_precede_ranking(monkeypatch):
    import trading_bot.decision.engine as module
    candidates = (assessment("breakout_retest", 95), assessment("trend_continuation", 75))
    monkeypatch.setattr(module, "evaluate_strategies", lambda *a, **kw: candidates)
    def gate(item):
        return replace(item, status=SetupStatus.NO_TRADE, signal=Signal.NO_TRADE) if item.strategy == "breakout_retest" else item
    result = DecisionEngine(candidate_gate=gate).evaluate(add_indicators(frame()))
    assert result.assessment.strategy == "trend_continuation"
    assert result.setup is not None


def test_spot_short_is_removed_before_ranking(monkeypatch):
    import trading_bot.decision.engine as module
    candidates = (replace(assessment("breakout_retest", 95), signal=Signal.SHORT), assessment("trend_continuation", 75))
    monkeypatch.setattr(module, "evaluate_strategies", lambda *a, **kw: candidates)
    result = DecisionEngine(allow_short=False).evaluate(add_indicators(frame()))
    assert result.assessment.signal == Signal.LONG
    assert result.assessment.strategy == "trend_continuation"


def test_pending_event_survives_absence_of_new_trigger(monkeypatch):
    def evaluate(history):
        d = decision("fresh-event")
        if len(history) > 2:
            d.assessment = replace(d.assessment, status=SetupStatus.FORMING)
            d.setup = None
        return d
    fake_engine(monkeypatch, evaluate)
    data = price_frame([(102,103,101,102), (102,103,101,102), (103,104,102,103),
                        (100,101,99,100), (102,103,101,102)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=0, slippage_bps=0)
    assert result.total_trades == 1
    assert result.trades.iloc[0].event_id == "fresh-event"


def test_same_event_does_not_reenter_after_stop(monkeypatch):
    fake_engine(monkeypatch, lambda history: decision("one-event"))
    data = price_frame([(100,101,99,100), (100,101,99,100), (100,101,94,95),
                        (100,101,94,95), (100,101,94,95), (100,101,94,95)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=0, slippage_bps=0)
    assert result.total_trades == 1
    assert result.execution_diagnostics["pending_created"] == 1


def test_pre_entry_high_cannot_automatically_take_profit(monkeypatch):
    fake_engine(monkeypatch, lambda history: decision())
    data = price_frame([(100,101,99,100), (100,101,99,100), (112,114,99,100)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=0, slippage_bps=0)
    assert result.total_trades == 1
    assert not bool(result.trades.iloc[0].tp1_hit)
    assert result.execution_diagnostics["ambiguous_bars"] == 1
    optimistic = run_decision_backtest(data, warmup_candles=1, fee_rate=0, slippage_bps=0, ambiguity_policy="optimistic")
    assert optimistic.final_equity >= result.final_equity


def test_fee_adjusted_break_even_covers_remaining_costs(monkeypatch):
    fake_engine(monkeypatch, lambda history: decision())
    data = price_frame([(100,101,99,100), (100,101,99,100), (100,101,99,100),
                        (102,106,102,105), (103,104,99,100)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=.001, slippage_bps=5)
    trade = result.trades.iloc[0]
    expected_first_half = ((105 * .9995 - trade.entry_price) - .001 * (105 * .9995 + trade.entry_price)) * trade.position_size / 2
    assert trade.pnl == pytest.approx(expected_first_half)
    assert trade.raw_gross_pnl - trade.slippage_cost - trade.entry_fee - trade.exit_fees == pytest.approx(trade.pnl)
    assert result.final_equity - result.initial_equity == pytest.approx(result.trades.pnl.sum())


def test_normal_stop_respects_cash_risk_budget_after_costs(monkeypatch):
    fake_engine(monkeypatch, lambda history: decision())
    data = price_frame([(100,101,99,100), (100,101,99,100), (100,101,94,95)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=.001, slippage_bps=5)
    assert result.trades.iloc[0].pnl == pytest.approx(-10.)


def test_exit_ablation_overrides_strategy_specific_profiles(monkeypatch):
    import trading_bot.research.exit_ablation as module
    configs = []
    def fake(data, **kwargs):
        configs.append(kwargs)
        return SimpleNamespace(trades=pd.DataFrame(), total_trades=0, total_return_pct=0,
                               max_drawdown_pct=0, profit_factor=0, win_rate_pct=0)
    monkeypatch.setattr(module, "run_decision_backtest", fake)
    rows, _ = module.run_exit_ablation(frame(), strategy_plan_configs={"trend_continuation_v2": TradePlanConfig(take_profit_1_fraction=.3, reward_to_risk_2=5)})
    assert [kw["strategy_plan_configs"]["trend_continuation_v2"].take_profit_1_fraction for kw in configs] == [0,.5,.5]
    assert all(kw["strategy_plan_configs"]["trend_continuation_v2"].reward_to_risk_2 == 2 for kw in configs)
    assert all("trend_continuation_v2" in value for value in rows.effective_strategy_configs)


def test_tp1_reversal_path_is_included_before_final_target(monkeypatch):
    fake_engine(monkeypatch, lambda history: decision())
    data = price_frame([(100,101,99,100), (100,101,99,100), (100,101,99,100), (101,111,100,110)])
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=0, slippage_bps=0)
    assert result.trades.iloc[0].pnl == pytest.approx(5.)
    assert result.trades.iloc[0].ambiguous_bars == 1


@pytest.mark.parametrize("short", [False, True])
def test_paper_and_backtest_share_ordered_execution_and_costs(monkeypatch, short):
    from trading_bot.paper import PaperBroker
    d = decision()
    data = price_frame([(100,101,99,100), (100,101,99,100), (100,101,99,100),
                        (102,106,102,105), (103,104,99,100)])
    if short:
        d.assessment = replace(d.assessment, signal=Signal.SHORT)
        d.setup = replace(d.setup, signal=Signal.SHORT, stop_loss=105., take_profit_1=95., take_profit_2=90.)
        for i, row in data.iterrows():
            data.loc[i, ["open", "high", "low", "close"]] = [200-row.open, 200-row.low, 200-row.high, 200-row.close]
    fake_engine(monkeypatch, lambda history: d)
    result = run_decision_backtest(data, warmup_candles=1, fee_rate=.001, slippage_bps=5)
    broker = PaperBroker(fee_rate=.001, slippage_bps=5)
    broker.submit_setup("BTC/USDT", "1h", d.setup, data.iloc[1].timestamp)
    for _, candle in data.iloc[2:].iterrows():
        broker.process_candle("BTC/USDT", "1h", candle)
    broker.close_all(data.iloc[-1].timestamp, "END_OF_DATA")
    assert broker.account_equity() == pytest.approx(result.final_equity)
    assert broker.completed_trades[0].net_pnl == pytest.approx(result.trades.iloc[0].pnl)


def test_event_dedupe_survives_journal_restart(tmp_path):
    from trading_bot.paper import PaperBroker, PaperJournal
    journal = PaperJournal(tmp_path / "events.db")
    broker = PaperBroker(journal=journal)
    order = broker.submit_setup("BTC/USDT", "1h", setup("persistent"), "2025-01-01T00:00:00Z")
    broker.cancel_order(order.order_id)
    restored = PaperBroker(journal=journal)
    restored.restore_from_journal()
    assert restored.has_setup_event("BTC/USDT", "1h", "persistent")
    with pytest.raises(ValueError, match="already consumed"):
        restored.submit_setup("BTC/USDT", "1h", setup("persistent"), "2025-01-02T00:00:00Z")


def test_paper_speculative_paths_do_not_duplicate_journal_writes(tmp_path):
    from trading_bot.paper import PaperBroker, PaperJournal
    journal = PaperJournal(tmp_path / "paths.db")
    broker = PaperBroker(journal=journal, fee_rate=0, slippage_bps=0)
    broker.submit_setup("BTC/USDT", "1h", setup(), "2025-01-01T00:00:00Z")
    for _, candle in price_frame([(100,101,99,100), (101,111,100,110)]).iterrows():
        broker.process_candle("BTC/USDT", "1h", candle)
    assert len(journal.all_trades()) == 1
    assert journal.all_trades().iloc[0].net_pnl == pytest.approx(5.)
