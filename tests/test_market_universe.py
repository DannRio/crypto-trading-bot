import pandas as pd
import pytest

from trading_bot.scanner import (
    DynamicMarketUniverse,
    UniverseConfig,
)

NOW = pd.Timestamp("2026-11-12 12:15:00", tz="UTC")


def candles(count, include_open=True):
    end = NOW.floor("h") - (pd.Timedelta(0) if include_open else pd.Timedelta(hours=1))
    times = pd.date_range(end=end, periods=count, freq="h")
    return [[int(stamp.timestamp() * 1000), 1.0, 1.0, 1.0, 1.0, 1.0] for stamp in times]


class FakeClient:
    def __init__(self):
        self.markets = {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "base": "BTC",
                "quote": "USDT",
                "type": "spot",
                "spot": True,
                "active": True,
            },
            "ETH/USDT": {
                "symbol": "ETH/USDT",
                "base": "ETH",
                "quote": "USDT",
                "type": "spot",
                "spot": True,
                "active": True,
            },
            "SOL/USDT": {
                "symbol": "SOL/USDT",
                "base": "SOL",
                "quote": "USDT",
                "type": "spot",
                "spot": True,
                "active": False,
            },
            "DOGE/USDC": {
                "symbol": "DOGE/USDC",
                "base": "DOGE",
                "quote": "USDC",
                "type": "spot",
                "spot": True,
                "active": True,
            },
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "base": "BTC",
                "quote": "USDT",
                "type": "swap",
                "swap": True,
                "active": True,
            },
        }
        self.tickers = {
            "BTC/USDT": {
                "quoteVolume": 50_000_000,
                "last": 100_000,
                "bid": 99_990,
                "ask": 100_010,
            },
            "ETH/USDT": {
                "quoteVolume": 25_000_000,
                "last": 3_000,
                "bid": 2_990,
                "ask": 3_010,
            },
        }

    def load_markets(self, reload=False):
        return self.markets

    def fetch_tickers(self, symbols=None):
        return {
            symbol: self.tickers[symbol]
            for symbol in symbols or []
            if symbol in self.tickers
        }

    def fetch_ticker(self, symbol):
        return self.tickers[symbol]

    def fetch_ohlcv(
        self,
        symbol,
        timeframe="1h",
        limit=500,
        since=None,
        params=None,
    ):
        return candles(limit)


def test_universe_filters_and_ranks_by_quote_liquidity():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=FakeClient(),
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=2,
            min_quote_volume=1_000_000,
        )
    )

    assert selection.symbols == [
        "BTC/USDT",
        "ETH/USDT",
    ]
    assert selection.candidates[0].quote_volume == 50_000_000
    assert selection.rejected["inactive"] == 1
    assert selection.rejected["quote"] == 1
    assert selection.rejected["market_type"] == 1


def test_universe_can_reject_wide_spreads():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=FakeClient(),
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=10,
            max_spread_bps=20,
        )
    )

    assert selection.symbols == ["BTC/USDT"]
    assert selection.rejected["wide_spread"] == 1


class DerivedVolumeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.tickers["BTC/USDT"] = {
            "quoteVolume": None,
            "baseVolume": 600,
            "last": 100_000,
            "bid": 99_990,
            "ask": 100_010,
        }


def test_universe_derives_quote_volume_when_needed():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=DerivedVolumeClient(),
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=1,
        )
    )

    assert selection.candidates[0].symbol == "BTC/USDT"
    assert selection.candidates[0].quote_volume == 60_000_000



class QualityFilterClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.markets.update(
            {
                "USDC/USDT": {
                    "symbol": "USDC/USDT",
                    "base": "USDC",
                    "quote": "USDT",
                    "type": "spot",
                    "spot": True,
                    "active": True,
                },
                "BTC3L/USDT": {
                    "symbol": "BTC3L/USDT",
                    "base": "BTC3L",
                    "quote": "USDT",
                    "type": "spot",
                    "spot": True,
                    "active": True,
                },
            }
        )
        self.tickers.update(
            {
                "USDC/USDT": {
                    "quoteVolume": 100_000_000,
                    "last": 1.0,
                    "bid": 0.9999,
                    "ask": 1.0001,
                },
                "BTC3L/USDT": {
                    "quoteVolume": 80_000_000,
                    "last": 10.0,
                    "bid": 9.99,
                    "ask": 10.01,
                },
            }
        )


def test_universe_excludes_stable_and_leveraged_bases():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=QualityFilterClient(),
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=10,
        )
    )

    assert "USDC/USDT" not in selection.symbols
    assert "BTC3L/USDT" not in selection.symbols
    assert selection.rejected["stable_base"] == 1
    assert selection.rejected["leveraged_token"] == 1


class MissingSpreadClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.tickers["ETH/USDT"] = {
            "quoteVolume": 25_000_000,
            "last": 3_000,
            "bid": None,
            "ask": None,
        }


def test_spread_cap_fails_closed_when_spread_is_missing():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=MissingSpreadClient(),
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=10,
            max_spread_bps=20,
        )
    )

    assert selection.symbols == ["BTC/USDT"]
    assert selection.rejected["missing_spread"] == 1


class HistoryClient(FakeClient):
    def fetch_ohlcv(
        self,
        symbol,
        timeframe="1h",
        limit=500,
        since=None,
        params=None,
    ):
        count = 50 if symbol == "BTC/USDT" else limit
        return candles(count)


def test_history_gate_skips_new_market_and_fills_next_candidate():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=HistoryClient(),
        now=lambda: NOW,
    )

    selection = selector.select(
        UniverseConfig(
            market_type="spot",
            quote_currencies=("USDT",),
            max_markets=1,
            min_history_candles=220,
            history_timeframe="1h",
            max_history_probes=2,
        )
    )

    assert selection.symbols == ["ETH/USDT"]
    assert selection.candidates[0].history_candles == 220
    assert selection.rejected["insufficient_history"] == 1


@pytest.mark.parametrize("kwargs", [
    {"min_quote_volume": float("nan")},
    {"min_quote_volume": float("inf")},
    {"min_quote_volume": True},
    {"min_quote_volume": "100"},
    {"max_spread_bps": float("nan")},
    {"max_spread_bps": float("inf")},
    {"max_markets": 1.5},
    {"max_markets": True},
    {"max_ticker_requests": 0},
    {"min_history_candles": 1.5},
    {"quote_currencies": (" ",)},
    {"quote_currencies": "USDT"},
    {"history_timeframe": "0h"},
    {"history_timeframe": "1M"},
    {"market_type": "option"},
    {"unique_base_assets": "false"},
])
def test_universe_rejects_invalid_config(kwargs):
    with pytest.raises(ValueError):
        UniverseConfig(**kwargs)


def test_metadata_empty_universe_makes_no_ticker_requests():
    class EmptyClient(FakeClient):
        def fetch_tickers(self, symbols=None):
            raise AssertionError("Empty symbols must not request all exchange tickers")

        def fetch_ticker(self, symbol):
            raise AssertionError("No eligible metadata")

    selection = DynamicMarketUniverse("fake", "spot", client=EmptyClient()).select(
        UniverseConfig(quote_currencies=("EUR",))
    )
    assert not selection.symbols
    assert selection.eligible_markets == 0


def test_each_discovery_refreshes_listing_and_active_metadata():
    class RefreshingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.reloads = []

        def load_markets(self, reload=False):
            self.reloads.append(reload)
            if reload and len(self.reloads) == 2:
                self.markets["BTC/USDT"]["active"] = False
                add_market(self, "SUI", volume=80_000_000)
            return self.markets

    client = RefreshingClient()
    selector = DynamicMarketUniverse("fake", "spot", client)
    assert selector.select().symbols == ["BTC/USDT", "ETH/USDT"]
    assert selector.select().symbols == ["SUI/USDT", "ETH/USDT"]
    assert client.reloads == [True, True]


def test_ticker_fallback_is_bounded_and_deterministic():
    class FallbackClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.requested = []

        def fetch_tickers(self, symbols=None):
            raise NotImplementedError

        def fetch_ticker(self, symbol):
            self.requested.append(symbol)
            return super().fetch_ticker(symbol)

    client = FallbackClient()
    selection = DynamicMarketUniverse("fake", "spot", client=client).select(
        UniverseConfig(max_ticker_requests=1)
    )
    assert client.requested == ["BTC/USDT"]
    assert selection.symbols == ["BTC/USDT"]
    assert selection.rejected["missing_volume"] == 1
    assert any("limit" in warning for warning in selection.warnings)


def test_partial_bulk_tickers_use_single_market_fallback():
    class PartialClient(FakeClient):
        def fetch_tickers(self, symbols=None):
            return {"BTC/USDT": self.tickers["BTC/USDT"]}

    selection = DynamicMarketUniverse("fake", "spot", client=PartialClient()).select()
    assert selection.symbols == ["BTC/USDT", "ETH/USDT"]
    assert any("omitted" in warning for warning in selection.warnings)


def test_current_unclosed_candle_does_not_complete_required_history():
    class LimitedClient(FakeClient):
        def fetch_ohlcv(self, *args, **kwargs):
            # Venue capped its response at 220, including the still-open candle.
            return candles(220)

    selection = DynamicMarketUniverse("fake", "spot", LimitedClient(), now=lambda: NOW).select(
        UniverseConfig(min_history_candles=220)
    )
    assert not selection.symbols
    assert selection.rejected["insufficient_history"] == 2


@pytest.mark.parametrize("mutation", ["duplicate", "gap", "unsorted", "misaligned", "nan", "bad_ohlc", "future", "short_row"])
def test_history_preflight_rejects_malformed_data_without_repair(mutation):
    class InvalidClient(FakeClient):
        def fetch_ohlcv(self, *args, **kwargs):
            rows = candles(222)
            if mutation == "duplicate":
                rows[10] = rows[9].copy()
            elif mutation == "gap":
                rows.pop(10)
            elif mutation == "unsorted":
                rows[10], rows[11] = rows[11], rows[10]
            elif mutation == "misaligned":
                rows[10][0] += 1000
            elif mutation == "nan":
                rows[10][4] = float("nan")
            elif mutation == "bad_ohlc":
                rows[10][2] = 0.5
            elif mutation == "future":
                rows.append([rows[-1][0] + 3_600_000, 1, 1, 1, 1, 1])
            else:
                rows[10] = rows[10][:3]
            return rows

    selection = DynamicMarketUniverse("fake", "spot", InvalidClient(), now=lambda: NOW).select(
        UniverseConfig(min_history_candles=220)
    )
    assert not selection.symbols
    assert selection.rejected["invalid_history"] == 2


def test_history_preflight_rejects_stale_but_long_enough_history():
    class StaleClient(FakeClient):
        def fetch_ohlcv(self, *args, **kwargs):
            return [[row[0] - 7_200_000, *row[1:]] for row in candles(221)]

    selection = DynamicMarketUniverse("fake", "spot", StaleClient(), now=lambda: NOW).select(
        UniverseConfig(min_history_candles=220)
    )
    assert not selection.symbols
    assert selection.rejected["stale_history"] == 2


def add_market(client, base, quote="USDT", volume=10_000_000, **metadata):
    symbol = metadata.pop("symbol", f"{base}/{quote}")
    client.markets[symbol] = dict(symbol=symbol, base=base, quote=quote, type="spot", active=True, **metadata)
    client.tickers[symbol] = dict(quoteVolume=volume, last=10, bid=9.99, ask=10.01)
    return symbol


def test_expanded_universe_covers_new_assets_without_suffix_false_positives():
    client = FakeClient()
    extras = ["JUP", "SUI", "LINK", "AVAX", "XRP", "ADA", "DOT", "NEAR", "OP", "ARB", "APT", "ATOM"]
    for base in extras:
        add_market(client, base)
    add_market(client, "BTCUP", volume=90_000_000)
    selection = DynamicMarketUniverse("fake", "spot", client).select()
    assert set(extras) <= {candidate.base for candidate in selection.candidates}
    assert "BTCUP/USDT" not in selection.symbols
    assert selection.rejected["leveraged_token"] == 1
    # Equal-volume candidates remain in a stable symbol order.
    assert selection.symbols[2:] == sorted(f"{base}/USDT" for base in extras)


def test_universe_deduplicates_base_but_can_opt_out():
    client = FakeClient()
    add_market(client, "BTC", quote="USDC", volume=80_000_000)
    selector = DynamicMarketUniverse("fake", "spot", client)
    config = dict(quote_currencies=("USDT", "USDC"), max_markets=2)
    selection = selector.select(UniverseConfig(**config))
    assert selection.symbols == ["BTC/USDC", "ETH/USDT"]
    assert selection.rejected["duplicate_base"] == 1
    assert selector.select(UniverseConfig(**config, unique_base_assets=False)).symbols == ["BTC/USDC", "BTC/USDT"]


def test_history_failure_on_first_quote_can_fall_back_to_second_quote():
    class QuoteHistoryClient(FakeClient):
        def fetch_ohlcv(self, symbol, **kwargs):
            return candles(50 if symbol == "BTC/USDC" else kwargs["limit"])

    client = QuoteHistoryClient()
    add_market(client, "BTC", quote="USDC", volume=80_000_000)
    selection = DynamicMarketUniverse("fake", "spot", client, now=lambda: NOW).select(
        UniverseConfig(quote_currencies=("USDT", "USDC"), max_markets=1, min_history_candles=220)
    )
    assert selection.symbols == ["BTC/USDT"]
    assert selection.rejected["insufficient_history"] == 1


def test_contract_discovery_requires_known_linear_quote_settlement():
    client = FakeClient()
    client.markets.clear()
    client.tickers.clear()
    variants = [
        ("BTC", dict(linear=True, inverse=False, settle="USDT")),
        ("ETH", dict(linear=False, inverse=True, settle="ETH")),
        ("SOL", {}),
        ("XRP", dict(linear=True, inverse=False, settle="USDC")),
    ]
    for base, meta in variants:
        symbol = f"{base}/USDT:USDT"
        client.markets[symbol] = dict(symbol=symbol, base=base, quote="USDT", type="swap", active=True, **meta)
        client.tickers[symbol] = dict(quoteVolume=10_000_000, last=10, bid=9.99, ask=10.01)
    selector = DynamicMarketUniverse("fake", "swap", client)
    selection = selector.select(UniverseConfig(market_type="swap"))
    assert selection.symbols == ["BTC/USDT:USDT"]
    assert selection.rejected["unsupported_contract"] == 3
    assert len(selector.select(UniverseConfig(market_type="swap", linear_contracts_only=False)).symbols) == 4
