"""Prove historical downloads cannot silently masquerade as multi-year data."""
import numpy as np
import pandas as pd
import pytest

from trading_bot.data import (
    HistoricalDataStore,
    IncompleteHistoryError,
    assess_history_coverage,
    download_history,
)
from trading_bot.research.evaluation import (
    run_out_of_sample_evaluation,
    run_walk_forward_evaluation,
)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=500, since=None, params=None):
        return [row for row in self.rows if row[0] >= since][:limit]


def daily_candles(start="2023-01-01", end="2026-01-01"):
    ts = pd.date_range(start=start, end=end, freq="D", inclusive="left", tz="UTC")
    prices = 100 + np.arange(len(ts)) * 0.05 + np.sin(np.arange(len(ts))/12)
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices + 1.5,
        "low": prices - 1.5,
        "close": prices + 0.1,
        "volume": np.full(len(ts), 1000.0),
    })


def raw_candles(frame):
    return [
        [int(ts.timestamp()*1000), float(o), float(h), float(l), float(c), float(v)]
        for ts, o, h, l, c, v in frame.itertuples(index=False, name=None)
    ]


def test_three_years_of_daily_candles_report_complete_coverage():
    df = daily_candles()
    report = assess_history_coverage(df, "2023-01-01", "2026-01-01", "1d")
    assert report.complete
    assert report.expected_candles == len(df)
    assert report.missing_candles == 0


def test_partial_coverage_detects_missing_front_back_and_middle():
    df = daily_candles("2023-01-01", "2023-02-01")
    partial = df.iloc[4:-3].drop(index=df.index[12])
    coverage = assess_history_coverage(partial, "2023-01-01", "2023-02-01", "1d")
    assert not coverage.complete
    assert coverage.missing_candles == 8


def test_download_rejects_exchange_history_cutoff_without_saving(tmp_path):
    frame = daily_candles("2025-12-01", "2026-01-01")
    store = HistoricalDataStore(tmp_path)
    client = FakeClient(raw_candles(frame))
    with pytest.raises(IncompleteHistoryError, match="incomplete history"):
        download_history(
            symbol="SOL/USDT", start="2023-01-01", end="2026-01-01",
            timeframe="1d", exchange_id="bybit", market_type="spot",
            client=client, store=store, batch_limit=10,
        )
    path = store.dataset_path("bybit", "spot", "SOL/USDT", "1d")
    assert not path.exists()


def test_partial_history_requires_explicit_override(tmp_path):
    frame = daily_candles("2025-12-01", "2026-01-01")
    store = HistoricalDataStore(tmp_path)
    saved = download_history(
        symbol="SOL/USDT", start="2023-01-01", end="2026-01-01",
        timeframe="1d", exchange_id="bybit", market_type="spot",
        client=FakeClient(raw_candles(frame)), store=store,
        strict_coverage=False, batch_limit=10,
    )
    assert len(saved) == len(frame)
    assert len(store.load("bybit", "spot", "SOL/USDT", "1d")) == len(frame)


def test_multi_year_train_validation_test_and_walk_forward_are_chronological():
    df = daily_candles()
    summary, details = run_out_of_sample_evaluation(
        data=df, train_cutoff="2024-01-01", validation_cutoff="2025-01-01",
        allow_short=False,
    )
    assert list(summary["segment"]) == ["TRAIN", "VALIDATION", "TEST"]
    assert all(summary["rows"] >= 365)
    assert summary.iloc[0]["end"] < summary.iloc[1]["start"]
    assert summary.iloc[1]["end"] < summary.iloc[2]["start"]
    assert not summary[["return_pct", "max_drawdown_pct"]].isna().any().any()
    results, windows = run_walk_forward_evaluation(
        df, train_bars=365, test_bars=180, step_bars=180,
        allow_short=False,
    )
    assert len(results) >= 2
    assert len(windows) == len(results)
    assert all(results["train_end"] < results["test_start"])
