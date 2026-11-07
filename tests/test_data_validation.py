import pandas as pd

from trading_bot.data.validation import validate_ohlcv


def make_data(rows: int = 250) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    close = pd.Series([100 + i * 0.1 for i in range(rows)])
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000.0,
        }
    )


def test_valid_ohlcv_passes():
    report = validate_ohlcv(make_data(), timeframe="1h", min_rows=220)
    assert report.valid
    assert report.missing_intervals == 0


def test_duplicate_timestamp_fails():
    df = make_data()
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    report = validate_ohlcv(df, timeframe="1h")
    assert not report.valid
    assert report.duplicate_timestamps == 1


def test_gap_is_reported_as_warning():
    df = make_data().drop(index=100).reset_index(drop=True)
    report = validate_ohlcv(df, timeframe="1h")
    assert report.valid
    assert report.missing_intervals == 1
    assert report.warnings
