from datetime import datetime, timezone
import json
from threading import RLock
from types import SimpleNamespace

import pytest

from trading_bot.analysis.markets import assess_liquidity, resolve_market
from trading_bot.exchanges.ccxt_client import CCXTClient


NOW = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def market(base="QNT", quote="USDT", **changes):
    result = {
        "symbol": f"{base}/{quote}", "id": f"{base}-{quote}",
        "base": base, "baseId": base, "quote": quote, "type": "spot",
        "spot": True, "contract": False, "active": True,
    }
    result.update(changes)
    return result


class PublicClient:
    def __init__(self, markets, currencies=None):
        self.markets = {m["symbol"]: m for m in markets}
        self.exchange = SimpleNamespace(currencies=currencies or {})
        self.reloaded = None

    def load_markets(self, reload=False):
        self.reloaded = reload
        return self.markets

    def fetch_currencies(self):
        return self.exchange.currencies


def snapshots(symbol="QNT/USDT"):
    ticker = {"symbol": symbol, "quoteVolume": 5_000_000, "timestamp": NOW_MS - 1000}
    book = {
        "symbol": symbol, "timestamp": NOW_MS - 1000,
        "bids": [[99.95, 5], [99.90, 10]],
        "asks": [[100.05, 5], [100.10, 10]],
    }
    return ticker, book


def test_full_name_uses_exchange_currency_identity_and_refreshes_metadata():
    client = PublicClient([market()], {"QNT": {"name": "QNT", "info": {"fullName": "Quant"}}})
    result = resolve_market("  Quant  ", client=client)
    assert result["status"] == "resolved"
    assert result["market"]["symbol"] == "QNT/USDT"
    assert result["coin_name"] == "Quant"
    assert result["short_supported"] is False
    assert client.reloaded is True


@pytest.mark.parametrize("query", ["QNT", "qnt/usdt", "QNT-USDT"])
def test_ticker_pair_and_native_id_resolve_without_name_catalog(query):
    assert resolve_market(query, client=PublicClient([market()]))["status"] == "resolved"


def test_partial_name_is_not_silently_guessed():
    client = PublicClient([market()], {"QNT": {"name": "Quant"}})
    result = resolve_market("Quan", client=client)
    assert result["status"] == "unavailable"
    assert result["market"] is None


def test_duplicate_full_names_require_an_explicit_pair():
    client = PublicClient([market("ABC"), market("XYZ")], {
        "ABC": {"name": "Example"}, "XYZ": {"name": "Example"},
    })
    result = resolve_market("Example", client=client)
    assert result["status"] == "ambiguous"
    assert result["market"] is None
    assert len(result["candidates"]) == 2
    assert resolve_market("ABC/USDT", client=client)["market"]["base"] == "ABC"


def test_other_quote_is_disclosed_without_replacing_requested_quote():
    result = resolve_market("QNT", client=PublicClient([market(quote="USDC")]))
    assert result["status"] == "unavailable"
    assert "USDC" in result["reasons"][0]


def test_perpetual_uses_native_metadata_and_distinct_kucoin_adapter():
    contract = market("BTC", symbol="BTC/USDT:USDT", id="XBTUSDTM", spot=False,
                      type="swap", swap=True, contract=True, linear=True,
                      inverse=False, settle="USDT", contractSize=0.001)
    client = PublicClient([contract], {"BTC": {"name": "Bitcoin"}})
    result = resolve_market("Bitcoin", market_type="swap", client=client)
    assert result["data_exchange_id"] == "kucoinfutures"
    assert result["market"]["symbol"] == "BTC/USDT:USDT"
    assert result["market"]["id"] == "XBTUSDTM"
    assert result["short_supported"] is True
    assert resolve_market("BTC", market_type="spot", client=client)["status"] == "unavailable"


def test_instrument_metadata_errors_do_not_leak_exception_details():
    class Failing(PublicClient):
        def load_markets(self, reload=False):
            raise RuntimeError("private header token")
    result = resolve_market("QNT", client=Failing([]))
    assert result["status"] == "unavailable"
    assert "private header" not in str(result)


def test_client_initialization_failure_returns_unavailable(monkeypatch):
    def unavailable(*args):
        raise RuntimeError("private configuration details")
    monkeypatch.setattr("trading_bot.analysis.markets.get_public_client", unavailable)
    result = resolve_market("QNT")
    assert result["status"] == "unavailable"
    assert "private configuration" not in str(result)


def test_name_catalog_failure_still_allows_exact_symbols():
    class WithoutCatalog(PublicClient):
        def fetch_currencies(self):
            raise NotImplementedError
    result = resolve_market("QNT/USDT", client=WithoutCatalog([market()]))
    assert result["status"] == "resolved"
    assert result["warnings"]


def test_jup_is_not_classified_as_leveraged_but_real_btcup_is():
    client = PublicClient([market("JUP"), market("BTC"), market("BTCUP")])
    assert resolve_market("JUP", client=client)["market"]["_analysis_leveraged_base"] is False
    assert resolve_market("BTCUP", client=client)["market"]["_analysis_leveraged_base"] is True


@pytest.mark.parametrize("query", ["", "x" * 121, "QNT\n", None])
def test_invalid_queries_are_rejected(query):
    with pytest.raises(ValueError):
        resolve_market(query, client=PublicClient([]))


def test_order_book_vwap_and_both_sides_are_measured_for_requested_notional():
    ticker, book = snapshots()
    result = assess_liquidity(market(), ticker, book, 1000, NOW)
    assert result["status"] == "pass"
    assert result["spread_bps"] == pytest.approx(10)
    assert result["buy_fill"]["vwap"] == pytest.approx(100.075)
    assert result["sell_fill"]["vwap"] == pytest.approx(99.925)
    assert result["buy_fill"]["slippage_bps"] == pytest.approx(7.5)
    assert result["buy_fill"]["requested_base_quantity"] == pytest.approx(10)
    assert result["freshness"]["order_book"]["source"] == "exchange"


def test_large_notional_cannot_pass_on_high_reported_turnover_alone():
    ticker, book = snapshots()
    result = assess_liquidity(market(), ticker, book, 100_000, NOW)
    assert result["status"] == "fail"
    assert result["buy_fill"]["complete"] is False
    assert result["sell_fill"]["complete"] is False


def test_contract_depth_converts_contract_counts_to_base_units():
    contract = market(symbol="QNT/USDT:USDT", spot=False, contract=True, type="swap",
                      swap=True, linear=True, inverse=False, settle="USDT", contractSize=0.001)
    ticker, book = snapshots(contract["symbol"])
    result = assess_liquidity(contract, ticker, book, 1000, NOW)
    assert result["status"] == "fail"
    assert result["buy_fill"]["base_quantity"] == pytest.approx(0.015)
    book["asks"] = [[100.05, 15_000]]
    book["bids"] = [[99.95, 15_000]]
    assert assess_liquidity(contract, ticker, book, 1000, NOW)["status"] == "pass"


@pytest.mark.parametrize("changes, status", [
    ({"active": False}, "fail"),
    ({"active": None}, "unavailable"),
    ({"base": "USDC"}, "fail"),
    ({"base": "BTC3L"}, "fail"),
    ({"_analysis_leveraged_base": True}, "fail"),
    ({"spot": False, "type": "swap", "contract": True, "linear": False, "inverse": True}, "unavailable"),
])
def test_ineligible_or_unknown_metadata_never_passes(changes, status):
    ticker, book = snapshots()
    assert assess_liquidity(market(**changes), ticker, book, 100, NOW)["status"] == status


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, -1])
def test_invalid_quote_volume_is_unavailable(value):
    ticker, book = snapshots()
    ticker["quoteVolume"] = value
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"


def test_low_volume_and_wide_spread_fail_independently():
    ticker, book = snapshots()
    ticker["quoteVolume"] = 999
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "fail"
    ticker["quoteVolume"] = 5_000_000
    book["bids"] = [[99, 100]]
    book["asks"] = [[101, 100]]
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "fail"
    assert any("spread" in reason for reason in result["reasons"])


def test_separate_volume_retains_its_own_receipt_provenance():
    ticker, book = snapshots()
    ticker["quoteVolume"] = None
    ticker["_quote_volume_snapshot"] = {
        "symbol": "QNT/USDT", "quoteVolume": 5_000_000,
        "timestamp": None, "_received_at": NOW, "source_method": "fetch_tickers",
    }
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "pass"
    assert result["freshness"]["ticker"]["source"] == "exchange"
    assert result["freshness"]["quote_volume"]["source"] == "receipt"
    assert result["quote_volume_source"]["observation_time_known"] is False
    assert any("observation time" in warning for warning in result["warnings"])


def test_stale_separate_volume_cannot_borrow_live_ticker_freshness():
    ticker, book = snapshots()
    ticker["quoteVolume"] = None
    ticker["_quote_volume_snapshot"] = {
        "symbol": "QNT/USDT", "quoteVolume": 5_000_000,
        "timestamp": NOW_MS - 300_000, "_received_at": NOW, "source_method": "fetch_tickers",
    }
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "unavailable"
    assert "Quote volume is stale." in result["reasons"]


@pytest.mark.parametrize("volume", [0, 100])
def test_reported_zero_or_low_volume_is_never_replaced_with_fallback(volume):
    ticker, book = snapshots()
    ticker["quoteVolume"] = volume
    ticker["_quote_volume_snapshot"] = {
        "symbol": "QNT/USDT", "quoteVolume": 5_000_000,
        "timestamp": NOW_MS, "source_method": "fetch_tickers",
    }
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "fail"
    assert result["quote_volume"] == volume
    assert result["quote_volume_source"]["source_method"] == "fetch_ticker"


def test_known_stale_source_time_cannot_be_overridden_with_fresh_receipt():
    ticker, book = snapshots()
    book["timestamp"] = NOW_MS - 300_000
    book["_received_at"] = NOW
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "unavailable"
    assert result["freshness"]["order_book"]["source"] == "exchange"
    assert "Order book is stale." in result["reasons"]


def test_missing_source_time_requires_and_discloses_bounded_receipt():
    ticker, book = snapshots()
    del book["timestamp"]
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"
    book["_received_at"] = NOW
    result = assess_liquidity(market(), ticker, book, 100, NOW)
    assert result["status"] == "pass"
    assert result["freshness"]["order_book"]["source"] == "receipt"
    assert any("receipt" in warning for warning in result["warnings"])
    book["_received_at"] = NOW_MS - 300_000
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"


@pytest.mark.parametrize("timestamp", [NOW_MS + 60_000, float("nan"), True, "invalid"])
def test_future_or_invalid_source_time_is_unavailable(timestamp):
    ticker, book = snapshots()
    book["timestamp"] = timestamp
    book["_received_at"] = NOW
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"


@pytest.mark.parametrize("bids", [[], [[99, -1]], [[99, True]], [[99, float("nan")]],
                                    [[99, 1], [100, 1]], [[99, 1], [99, 2]], [[101, 1]]])
def test_corrupt_or_crossed_order_book_cannot_produce_a_pass(bids):
    ticker, book = snapshots()
    book["bids"] = bids
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"


def test_a_different_instrument_snapshot_is_rejected():
    ticker, book = snapshots("BTC/USDT")
    assert assess_liquidity(market(), ticker, book, 100, NOW)["status"] == "unavailable"


def test_extreme_book_values_cannot_emit_nonfinite_json_or_an_eligible_signal():
    ticker, book = snapshots()
    book["bids"] = [[1e-308, 1]]
    book["asks"] = [[1e-308, 0.0001], [1e308, 0.01]]
    result = assess_liquidity(market(), ticker, book, 1e-310, NOW)
    assert result["status"] == "unavailable"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("notional", [0, -1, True, float("inf"), float("nan")])
def test_nonfinite_or_invalid_notional_is_rejected(notional):
    ticker, book = snapshots()
    with pytest.raises(ValueError):
        assess_liquidity(market(), ticker, book, notional, NOW)


def test_public_methods_are_explicitly_capability_checked():
    client = CCXTClient.__new__(CCXTClient)
    client.exchange_id = "test"
    client._request_lock = RLock()
    client.exchange = SimpleNamespace(has={})
    for method, arguments in (("fetch_currencies", ()), ("fetch_order_book", ("QNT/USDT",)),
                              ("fetch_funding_rate", ("QNT/USDT:USDT",)),
                              ("fetch_open_interest", ("QNT/USDT:USDT",))):
        with pytest.raises(NotImplementedError):
            getattr(client, method)(*arguments)


def test_public_order_book_method_preserves_exchange_result_and_limit():
    calls = []
    client = CCXTClient.__new__(CCXTClient)
    client.exchange_id = "test"
    client._request_lock = RLock()
    client.exchange = SimpleNamespace(
        has={"fetchOrderBook": True},
        fetch_order_book=lambda symbol, limit: calls.append((symbol, limit)) or {"asks": []},
    )
    assert client.fetch_order_book("QNT/USDT", limit=20) == {"asks": []}
    assert calls == [("QNT/USDT", 20)]
