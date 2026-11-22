import pandas as pd
import pytest

from trading_bot.data import HistoricalDataStore, download_history, fetch_ohlcv_range
from trading_bot.data.market_data import OHLCV_COLUMNS
from trading_bot.data.validation import validate_ohlcv


def _row(hour):
    stamp = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=hour)
    return [int(stamp.timestamp() * 1000), 100.0, 102.0, 99.0, 101.0, 1000.0]


def _frame(rows):
    data = pd.DataFrame(rows, columns=OHLCV_COLUMNS)
    data["timestamp"] = pd.to_datetime(data["timestamp"], unit="ms", utc=True)
    return data


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)

    def fetch_ohlcv(self, **kwargs):
        return next(self.pages, [])


def _fetch(pages, **kwargs):
    return fetch_ohlcv_range("SUI/USDT", start="2026-01-01",
                            end="2026-01-01T05:00:00Z", timeframe="1h",
                            client=Pages(pages), **kwargs)


def test_saved_csv_order_and_duplicates_reach_research_validation(tmp_path):
    store = HistoricalDataStore(tmp_path)
    path = store.dataset_path("fake", "spot", "SUI/USDT", "1h")
    path.parent.mkdir(parents=True)
    original = _frame([_row(2), _row(0), _row(0)])
    original.to_csv(path, index=False)
    loaded = store.load("fake", "spot", "SUI/USDT", "1h")
    assert loaded["timestamp"].tolist() == original["timestamp"].tolist()
    quality = validate_ohlcv(loaded, timeframe="1h")
    assert not quality.valid
    assert quality.duplicate_timestamps == 1
    assert any("sorted" in issue for issue in quality.issues)


@pytest.mark.parametrize("defect", ["duplicate", "unsorted", "gap", "nan", "invalid_ohlc", "timestamp", "misaligned_single"])
def test_download_rejects_raw_page_defects_before_normalization(defect):
    rows = [_row(0), _row(1), _row(2)]
    if defect == "duplicate":
        rows[1] = rows[0].copy()
    elif defect == "unsorted":
        rows[0], rows[1] = rows[1], rows[0]
    elif defect == "gap":
        rows.pop(1)
    elif defect == "nan":
        rows[1][5] = float("nan")
    elif defect == "invalid_ohlc":
        rows[1][2] = 98
    elif defect == "timestamp":
        rows[1][0] = "malformed"
    else:
        rows = [_row(0)]
        rows[0][0] += 1000
    with pytest.raises(ValueError, match="Invalid downloaded OHLCV page"):
        _fetch([rows])


def test_download_allows_cross_page_overlap_with_last_response_winning():
    replacement = _row(2)
    replacement[4] = 101.5
    result = _fetch([
        [_row(0), _row(1), _row(2)],
        [replacement, _row(3), _row(4)],
    ])
    assert len(result) == 5
    assert result["timestamp"].is_unique
    assert result.iloc[2]["close"] == 101.5


def test_download_rejects_missing_interval_between_valid_pages():
    with pytest.raises(ValueError, match="Invalid downloaded OHLCV range"):
        _fetch([[_row(0), _row(1)], [_row(3), _row(4)]])


def test_failed_download_leaves_existing_dataset_unchanged(tmp_path):
    store = HistoricalDataStore(tmp_path)
    path = store.upsert("fake", "spot", "SUI/USDT", "1h", _frame([_row(0)]))
    previous = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid downloaded OHLCV page"):
        download_history("SUI/USDT", start="2026-01-01", end="2026-01-01T05:00:00Z",
                         timeframe="1h", exchange_id="fake", store=store,
                         client=Pages([[_row(0), _row(1)], [_row(2), _row(2)]]))
    assert path.read_bytes() == previous


def test_upsert_last_write_wins_for_many_duplicate_keys(tmp_path):
    store = HistoricalDataStore(tmp_path)
    original = _frame([_row(hour) for hour in range(100)])
    store.upsert("fake", "spot", "SUI/USDT", "1h", original)
    updated = original.copy()
    updated["close"] = 101.5
    store.upsert("fake", "spot", "SUI/USDT", "1h", updated)
    loaded = store.load("fake", "spot", "SUI/USDT", "1h")
    assert len(loaded) == 100
    assert (loaded["close"] == 101.5).all()
