from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_bot.data import HistoricalDataStore
from trading_bot.research import batch, cli
from trading_bot.research.experiments import run_parameter_experiments
from trading_bot.research.models import DatasetSpec
from trading_bot.research.universe import load_universe_snapshot, save_universe_snapshot
from trading_bot.scanner import MarketCandidate, UniverseConfig, UniverseSelection


SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT"]


def candles(rows=280, timeframe="1h", price=100.0):
    return pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=rows, freq=timeframe, tz="UTC"),
                         "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1000.0})


def selection(symbols=SYMBOLS, market_type="spot"):
    return UniverseSelection(
        candidates=tuple(MarketCandidate(symbol, symbol.split("/")[0], "USDT", market_type,
                                         True, 10_000_000.0, 100.0, 99.99, 100.01, 2.0, 240)
                         for symbol in symbols),
        total_markets=500, eligible_markets=200,
        warnings=("Example bounded-discovery warning.",), rejected={"low_volume": 12},
    )


def snapshot(tmp_path, symbols=SYMBOLS, market_type="spot"):
    path = tmp_path / "universe.json"
    document = save_universe_snapshot(path, "kucoin", UniverseConfig(market_type=market_type),
                                      selection(symbols, market_type),
                                      selected_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    return path, document


def fake_result():
    return SimpleNamespace(trades=pd.DataFrame(), total_trades=0, total_return_pct=0.0,
                           max_drawdown_pct=0.0, win_rate_pct=0.0, profit_factor=0.0, average_pnl=0.0)


def test_universe_snapshot_freezes_more_than_three_assets_and_is_immutable(tmp_path):
    path, document = snapshot(tmp_path)
    before = path.read_bytes()
    loaded = load_universe_snapshot(path)
    assert loaded == document
    assert loaded["symbols"] == SYMBOLS
    assert loaded["selection"]["rejected"] == {"low_volume": 12}
    assert loaded["filters"]["unique_base_assets"] is True
    assert "survivorship" in loaded["warning"]
    assert len(loaded["snapshot_sha256"]) == 64
    with pytest.raises(FileExistsError):
        save_universe_snapshot(path, "kucoin", UniverseConfig(), selection())
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["symbols", "filters", "provenance"])
def test_frozen_universe_detects_edited_selection(tmp_path, change):
    path, document = snapshot(tmp_path)
    if change == "symbols":
        document["symbols"].reverse()
    elif change == "filters":
        document["filters"]["min_quote_volume"] = 1.0
    else:
        document["selected_at_utc"] = "2026-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_universe_snapshot(path)


def test_universe_command_discovers_and_records_filters(monkeypatch, tmp_path):
    seen = {}

    class Universe:
        def __init__(self, exchange, market_type):
            seen.update(exchange=exchange, market_type=market_type)

        def select(self, config):
            seen["config"] = config
            return selection()

    monkeypatch.setattr(cli, "DynamicMarketUniverse", Universe)
    path = tmp_path / "discovered.json"
    assert cli.main(["universe", "--exchange", "kucoin", "--max-markets", "30",
                     "--max-ticker-requests", "75", "--output", str(path)]) == 0
    document = load_universe_snapshot(path)
    assert seen["config"].max_markets == 30
    assert document["filters"]["max_ticker_requests"] == 75
    assert document["symbols"] == SYMBOLS


def test_download_reuses_exact_snapshot_without_rediscovery(monkeypatch, tmp_path):
    path, document = snapshot(tmp_path)
    seen = []

    def fetch(**kwargs):
        seen.append((kwargs["exchange_id"], kwargs["market_type"], kwargs["symbol"]))
        return candles(24)

    def rediscovery(*args, **kwargs):
        raise AssertionError("Frozen research must not reselect today's winners.")

    monkeypatch.setattr(cli, "DynamicMarketUniverse", rediscovery)
    monkeypatch.setattr(cli, "download_history", fetch)
    report = tmp_path / "download.csv"
    assert cli.main(["download", "--universe-file", str(path), "--start", "2025-01-01",
                     "--end", "2025-01-02", "--export", str(report)]) == 0
    assert seen == [("kucoin", "spot", symbol) for symbol in SYMBOLS]
    manifest = json.loads(report.with_suffix(".csv.manifest.json").read_text())
    assert manifest["universe_snapshot"]["snapshot_sha256"] == document["snapshot_sha256"]
    assert pd.read_csv(report)["status"].eq("OK").all()


def test_frozen_universe_cannot_silently_switch_venue(tmp_path):
    path, _ = snapshot(tmp_path)
    with pytest.raises(SystemExit, match="must match"):
        cli.main(["backtest", "--universe-file", str(path), "--exchange", "bybit"])


def test_download_reports_failure_and_continues_other_assets(monkeypatch, tmp_path):
    def fetch(**kwargs):
        if kwargs["symbol"] == "BTC/USDT":
            raise RuntimeError("exchange rejected historical request")
        return candles(12)

    monkeypatch.setattr(cli, "download_history", fetch)
    report = tmp_path / "download.csv"
    code = cli.main(["download", "--exchange", "kucoin", "--symbols", "BTC/USDT,LINK/USDT",
                     "--start", "2025-01-01", "--end", "2025-01-02", "--export", str(report)])
    rows = pd.read_csv(report)
    assert code == 1
    assert list(rows.status) == ["DOWNLOAD_ERROR", "PARTIAL_DATA"]
    assert list(rows.symbol) == ["BTC/USDT", "LINK/USDT"]


def test_backtest_cli_executes_real_batch_without_global_allow_short_mismatch(tmp_path):
    store = HistoricalDataStore(tmp_path / "history")
    store.upsert("kucoin", "spot", "ADA/USDT", "1h", candles())
    report = tmp_path / "batch.csv"
    code = cli.main(["backtest", "--exchange", "kucoin", "--symbols", "ADA/USDT",
                     "--root", str(store.root), "--export", str(report)])
    assert code == 0
    row = pd.read_csv(report).iloc[0]
    assert row.status == "OK"
    assert row.strategies == "trend_continuation_v2"
    assert row.short_trades == 0


def test_batch_preserves_missing_invalid_and_successful_assets(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path)
    store.upsert("kucoin", "spot", "ADA/USDT", "1h", candles())
    bad = candles()
    bad.loc[50, "close"] = float("nan")
    store.upsert("kucoin", "spot", "SOL/USDT", "1h", bad)
    monkeypatch.setattr(batch, "run_decision_backtest", lambda **kwargs: fake_result())
    specs = [DatasetSpec("kucoin", "spot", symbol, "1h")
             for symbol in ("BTC/USDT", "SOL/USDT", "ADA/USDT")]
    summary, details = batch.run_batch_research(specs, store=store)
    assert list(summary.status) == ["MISSING_DATA", "INVALID_DATA", "OK"]
    assert list(summary.symbol) == ["BTC/USDT", "SOL/USDT", "ADA/USDT"]
    assert len(details) == 1


def test_batch_enforces_spot_long_only_and_keeps_derivative_symbols(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path)
    specs = [DatasetSpec("kucoin", "spot", "ADA/USDT", "1h"),
             DatasetSpec("bybit", "swap", "ADA/USDT:USDT", "1h")]
    for spec in specs:
        store.upsert(spec.exchange_id, spec.market_type, spec.symbol, spec.timeframe, candles())
    seen = []

    def backtest(**kwargs):
        seen.append(kwargs["allow_short"])
        return fake_result()

    monkeypatch.setattr(batch, "run_decision_backtest", backtest)
    summary, _ = batch.run_batch_research(specs, store=store)
    assert seen == [False, True]
    assert summary.symbol.iloc[1] == "ADA/USDT:USDT"


def test_batch_passes_matching_secondary_data_for_each_symbol(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path)
    for symbol, price in (("ADA/USDT", 100), ("LINK/USDT", 200)):
        for timeframe in ("1h", "4h", "15min"):
            stored_timeframe = "15m" if timeframe == "15min" else timeframe
            store.upsert("kucoin", "spot", symbol, stored_timeframe, candles(timeframe=timeframe, price=price))
    seen = []

    def backtest(**kwargs):
        assert kwargs["primary_timeframe"] == "1h"
        assert kwargs["confirmation_timeframes"] == ("4h",)
        assert kwargs["entry_timeframe"] == kwargs["execution_timeframe"] == "15m"
        seen.append((kwargs["data"].close.iloc[0], kwargs["secondary_data"]["15m"].close.iloc[0]))
        return fake_result()

    monkeypatch.setattr(batch, "run_decision_backtest", backtest)
    specs = [DatasetSpec("kucoin", "spot", symbol, "1h") for symbol in ("ADA/USDT", "LINK/USDT")]
    summary, _ = batch.run_batch_research(specs, store, confirmation_timeframes=("4h",),
                                         entry_timeframe="15m", execution_timeframe="15m")
    assert summary.status.eq("OK").all()
    assert seen == [(100, 100), (200, 200)]


def test_missing_secondary_data_is_not_a_single_timeframe_fallback(monkeypatch, tmp_path):
    store = HistoricalDataStore(tmp_path)
    store.upsert("kucoin", "spot", "ADA/USDT", "1h", candles())
    monkeypatch.setattr(batch, "run_decision_backtest", lambda **kwargs: pytest.fail("Missing timeframe must prevent backtest"))
    summary, details = batch.run_batch_research([DatasetSpec("kucoin", "spot", "ADA/USDT", "1h")],
                                               store, confirmation_timeframes=("4h",))
    assert summary.status.iloc[0] == "MISSING_DATA"
    assert "4h" in summary.error.iloc[0]
    assert not details


@pytest.mark.parametrize("mode, extra", [
    ("oos", ["--train-cutoff", "2025-01-10", "--validation-cutoff", "2025-01-11"]),
    ("walk-forward", ["--train-bars", "220", "--test-bars", "20"]),
])
def test_segmented_cli_reports_missing_assets_and_forwards_gates(monkeypatch, tmp_path, mode, extra):
    store = HistoricalDataStore(tmp_path / "history")
    store.upsert("kucoin", "spot", "ADA/USDT", "1h", candles())
    store.upsert("kucoin", "spot", "ADA/USDT", "4h", candles(timeframe="4h"))
    seen = []

    def evaluate(**kwargs):
        seen.append(kwargs)
        return pd.DataFrame([{"trades": 0}]), {}

    function = "run_out_of_sample_evaluation" if mode == "oos" else "run_walk_forward_evaluation"
    monkeypatch.setattr(cli, function, evaluate)
    report = tmp_path / "evaluation.csv"
    code = cli.main([mode, "--exchange", "kucoin", "--symbols", "MISSING/USDT,ADA/USDT",
                     "--root", str(store.root), "--confirm-timeframes", "4h", "--export", str(report), *extra])
    rows = pd.read_csv(report)
    assert code == 1
    assert list(rows.status) == ["MISSING_DATA", "OK"]
    assert seen[0]["confirmation_timeframes"] == ("4h",)
    assert seen[0]["allow_short"] is False
    assert "4h" in seen[0]["secondary_data"]


def test_parameter_experiments_forward_non_hourly_primary_timeframe(monkeypatch):
    import trading_bot.research.experiments as experiments
    seen = []

    def backtest(**kwargs):
        seen.append(kwargs["primary_timeframe"])
        result = fake_result()
        result.final_equity = 10_000
        return result

    monkeypatch.setattr(experiments, "run_decision_backtest", backtest)
    result = run_parameter_experiments(candles(timeframe="4h"), {"min_confirmed_score": [70]},
                                       train_end="2026-01-01", primary_timeframe="4h")
    assert seen == ["4h"]
    assert len(result) == 1
