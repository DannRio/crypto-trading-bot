"""Advanced research profiles and public API integration without exchange requests."""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from trading_bot import cli as scan_cli
from trading_bot.config import apply_profile, load_profile
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.paper import PaperJournal
from trading_bot.paper import cli as paper_cli
from trading_bot.scanner import MarketCandidate, ScanResult, UniverseSelection
from trading_bot.strategies import SetupAssessment
from trading_bot.web import app as web


client = TestClient(web.app)


def write_profile(path, settings):
    path.write_text(json.dumps({"schema_version": 1, "name": "custom",
                                "description": "Integration test", "settings": settings}))
    return str(path)


def no_trade_result(symbol):
    return ScanResult(
        exchange_id="kraken", market_type="spot", symbol=symbol, timeframe="1h",
        assessment=SetupAssessment(signal=Signal.NO_TRADE, status=SetupStatus.NO_TRADE,
                                   score=0, reason="No qualifying setup", reasons=(),
                                   strategy="trend_continuation_v2"),
        structure=None, setup=None, data_quality=None,
    )


def scanner_fixture(captured):
    class Scanner:
        def __init__(self, **kwargs):
            captured["scanner"] = kwargs

        def scan(self, symbols):
            captured["symbols"] = list(symbols)
            return [no_trade_result(symbol) for symbol in symbols]

    return Scanner


def test_explicit_cli_overrides_profile_and_manual_watchlist_wins():
    tokens = ["--profile", "broad-spot", "--max-markets=12", "--risk-percent", "0.75",
              "--symbols", "ADA/USDT,LINK/USDT"]
    args = apply_profile(scan_cli.build_parser().parse_args(tokens), tokens)
    assert args.auto_universe is False
    assert args.symbols == "ADA/USDT,LINK/USDT"
    assert args.max_markets == 12
    assert args.risk_percent == 0.75
    assert args.confirm_timeframes == "4h"
    assert args.strategies == "trend_continuation_v2"
    # Modifying one resolved profile must not alter subsequent invocations.
    profile = load_profile("broad-spot")
    profile.settings["max_markets"] = 1
    assert load_profile("broad-spot").settings["max_markets"] == 50


def test_explicit_manual_mode_can_override_perpetual_preset():
    tokens = ["--profile", "liquid-perps", "--no-auto-universe", "--symbols", "LINK/USDT:USDT"]
    args = apply_profile(scan_cli.build_parser().parse_args(tokens), tokens)
    assert args.auto_universe is False
    assert args.market_type == "swap"
    assert args.symbols == "LINK/USDT:USDT"


def test_paper_cli_negative_boolean_overrides_custom_profile(monkeypatch, tmp_path):
    profile = write_profile(tmp_path / "paper.json", {
        "auto_universe": True, "max_markets": 40, "allow_missing_correlation_data": True,
    })
    captured = {}

    def daemon(args):
        captured.update(vars(args))
        return 0

    monkeypatch.setattr(paper_cli, "_daemon", daemon)
    assert paper_cli.main(["daemon", "--profile", profile, "--max-markets", "7",
                           "--no-allow-missing-correlation-data", "--once"]) == 0
    assert captured["allow_missing_correlation_data"] is False
    assert captured["max_markets"] == 7
    assert captured["auto_universe"] is True


@pytest.mark.parametrize("settings", [
    {"risk_percent": float("nan")},
    {"min_quote_volume": float("inf")},
    {"max_markets": 0},
    {"max_markets": True},
    {"auto_universe": "true"},
    {"fee_rate": -0.1},
    {"unknown_option": 1},
    {"api_key": "must-never-be-a-profile-setting"},
    {"secret": "must-never-be-a-profile-setting"},
])
def test_custom_profile_rejects_invalid_values_and_credentials(tmp_path, settings):
    path = write_profile(tmp_path / "invalid.json", settings)
    with pytest.raises(ValueError):
        load_profile(path)


def test_web_default_discovers_thirty_markets_and_exposes_coverage(monkeypatch):
    captured = {}

    class Universe:
        def __init__(self, **kwargs):
            captured["universe"] = kwargs

        def select(self, config):
            captured["config"] = config
            return UniverseSelection(
                candidates=tuple(MarketCandidate(
                    symbol=f"ASSET{i}/USDT", base=f"ASSET{i}", quote="USDT", market_type="spot",
                    active=True, quote_volume=10_000_000, last=100, bid=99.99, ask=100.01,
                    spread_bps=2, history_candles=250,
                ) for i in range(config.max_markets)),
                total_markets=500, eligible_markets=100,
                warnings=("Some markets lack closed history.",), rejected={"invalid_history": 2},
            )

    monkeypatch.setattr(web, "DynamicMarketUniverse", Universe)
    monkeypatch.setattr(web, "MarketScanner", scanner_fixture(captured))
    response = client.get("/api/scan")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["auto_universe"] is True
    assert captured["config"].max_markets == 30
    assert len(result["results"]) == 30
    assert len(set(captured["symbols"])) == 30
    assert result["universe"]["rejected"] == {"invalid_history": 2}
    assert result["universe"]["warnings"] == ["Some markets lack closed history."]
    assert captured["scanner"]["strategy_names"] == ("trend_continuation_v2",)


@pytest.mark.parametrize("count", [9, 100])
def test_manual_watchlists_expand_beyond_eight_without_discovery(monkeypatch, count):
    captured = {}

    def unexpected_discovery(**kwargs):
        pytest.fail("Explicit manual symbols must not trigger discovery.")

    symbols = [f"ASSET{i}/USDT" for i in range(count)]
    monkeypatch.setattr(web, "DynamicMarketUniverse", unexpected_discovery)
    monkeypatch.setattr(web, "MarketScanner", scanner_fixture(captured))
    response = client.get("/api/scan", params={"symbols": ",".join(symbols)})
    assert response.status_code == 200, response.text
    assert response.json()["auto_universe"] is False
    assert captured["symbols"] == symbols
    assert len(response.json()["results"]) == count


def test_manual_watchlist_over_limit_is_rejected_before_scanning(monkeypatch):
    def unexpected_scanner(**kwargs):
        pytest.fail("Oversized watchlist must be rejected before market access.")

    monkeypatch.setattr(web, "MarketScanner", unexpected_scanner)
    response = client.get("/api/scan", params={"symbols": ",".join(f"ASSET{i}/USDT" for i in range(101))})
    assert response.status_code == 422


def test_profile_endpoint_serializes_supported_presets():
    response = client.get("/api/profiles")
    assert response.status_code == 200
    body = response.json()
    profiles = {profile["name"]: profile for profile in body["profiles"]}
    assert profiles["liquid-spot"]["settings"]["max_markets"] == 30
    assert profiles["broad-spot"]["settings"]["max_markets"] == 50
    assert profiles["liquid-perps"]["settings"]["market_type"] == "swap"
    assert profiles["liquid-spot"]["settings"]["strategies"] == "trend_continuation_v2"
    assert body["live_execution"] is False
    assert all(profile["schema_version"] == 1 for profile in profiles.values())


def test_paper_status_exposes_persisted_universe_and_heartbeat(monkeypatch, tmp_path):
    path = tmp_path / "paper.db"
    universe = {"symbols": ["ADA/USDT", "LINK/USDT"], "available": False,
                "warnings": ["Refresh unavailable; existing positions remain managed."]}
    heartbeat = {"completed_at": "2026-11-01T12:00:00+00:00", "state": "degraded",
                 "managed_markets": 2, "scanned_markets": 0}
    journal = PaperJournal(path)
    try:
        journal.set_metadata("universe_status", json.dumps(universe))
        journal.set_metadata("heartbeat", json.dumps(heartbeat))
    finally:
        journal.close()
    monkeypatch.setattr(web, "PAPER_JOURNAL_PATH", path)
    response = client.get("/api/paper/status")
    assert response.status_code == 200, response.text
    assert response.json()["universe"] == universe
    assert response.json()["heartbeat"] == heartbeat
    assert response.json()["summary"]["open_positions"] == 0


def test_paper_status_handles_legacy_invalid_metadata(monkeypatch, tmp_path):
    path = tmp_path / "paper.db"
    journal = PaperJournal(path)
    try:
        journal.set_metadata("universe_status", "not-json")
        journal.set_metadata("heartbeat", "[]")
    finally:
        journal.close()
    monkeypatch.setattr(web, "PAPER_JOURNAL_PATH", path)
    response = client.get("/api/paper/status")
    assert response.status_code == 200, response.text
    assert response.json()["universe"] == {}
    assert response.json()["heartbeat"] == {}
