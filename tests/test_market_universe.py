from trading_bot.scanner import (
    DynamicMarketUniverse,
    UniverseConfig,
)


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
        return [
            [i, 1.0, 1.0, 1.0, 1.0, 1.0]
            for i in range(limit)
        ]


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
        return [
            [i, 1.0, 1.0, 1.0, 1.0, 1.0]
            for i in range(count)
        ]


def test_history_gate_skips_new_market_and_fills_next_candidate():
    selector = DynamicMarketUniverse(
        exchange_id="fake",
        market_type="spot",
        client=HistoryClient(),
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
