"""Offline archive import tests for multi-year market research."""
import pandas as pd
import pytest

from trading_bot.data import (
    HistoricalDataStore,
    IncompleteHistoryError,
    assess_history_coverage,
    import_history_csv,
)
from trading_bot.research.cli import main


def sample_frame(start="2024-01-01", end="2026-01-01"):
    timestamps = pd.date_range(start, end, inclusive="left", freq="D", tz="UTC")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [100.0] * len(timestamps),
        "high": [102.0] * len(timestamps),
        "low": [99.0] * len(timestamps),
        "close": [101.0] * len(timestamps),
        "volume": [1000.0] * len(timestamps),
    })


def test_import_two_year_archive_with_iso_timestamps(tmp_path):
    frame = sample_frame()
    filename = tmp_path / "archive.csv"
    frame.to_csv(filename, index=False)
    store = HistoricalDataStore(tmp_path / "history")
    report = import_history_csv(
        filename, "2024-01-01", "2026-01-01", "BTC/USDT",
        "1d", "kucoin", "spot", store=store,
    )
    assert report.complete
    assert len(store.load("kucoin", "spot", "BTC/USDT", "1d")) == len(frame)


def test_import_csv_accepts_unix_milliseconds(tmp_path):
    frame = sample_frame(start="2025-01-01", end="2025-01-06")
    frame["timestamp"] = frame["timestamp"].apply(lambda t: int(t.timestamp() * 1000))
    filename = tmp_path / "archive_ms.csv"
    frame.to_csv(filename, index=False)
    report = import_history_csv(
        filename, "2025-01-01", "2025-01-06", "SOL/USDT",
        "1d", "kucoin", "spot", store=HistoricalDataStore(tmp_path/"store"),
    )
    assert report.complete


def test_archive_missing_days_rejected_without_writing(tmp_path):
    frame = sample_frame(start="2025-01-01", end="2025-01-06")
    frame = frame.drop(frame.index[2])
    filename = tmp_path/"missing.csv"
    frame.to_csv(filename, index=False)
    store = HistoricalDataStore(tmp_path/"store")
    with pytest.raises(IncompleteHistoryError):
        import_history_csv(
            filename, "2025-01-01", "2025-01-06",
            "SOL/USDT", "1d", "kucoin", "spot", store=store,
        )
    assert not store.dataset_path("kucoin", "spot", "SOL/USDT", "1d").exists()


def test_archive_rejects_corrupt_price_values(tmp_path):
    frame = sample_frame(start="2025-01-01", end="2025-01-06")
    frame.loc[2, "high"] = 0
    filename = tmp_path/"broken.csv"
    frame.to_csv(filename, index=False)
    with pytest.raises(ValueError, match="invalid OHLCV"):
        import_history_csv(
            filename, "2025-01-01", "2025-01-06",
            "SOL/USDT", "1d", "kucoin", "spot",
            store=HistoricalDataStore(tmp_path/"store"),
        )


def test_weekly_candles_use_monday_utc_anchor():
    dates = pd.date_range("2026-01-05", periods=5, freq="W-MON", tz="UTC")
    report = assess_history_coverage(
        pd.DataFrame({"timestamp": dates}),
        "2026-01-05", "2026-02-09", "1w",
    )
    assert report.complete


def test_import_csv_cli_succeeds_offline(tmp_path):
    frame = sample_frame(start="2025-01-01", end="2025-01-06")
    filename = tmp_path/"market.csv"
    frame.to_csv(filename, index=False)
    result = main([
        "import-csv", "--exchange", "kucoin", "--market-type", "spot",
        "--symbol", "BTC/USDT", "--timeframe", "1d",
        "--file", str(filename), "--root", str(tmp_path/"stored"),
        "--start", "2025-01-01", "--end", "2025-01-06",
    ])
    assert result == 0
