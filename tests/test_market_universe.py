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
            "baseVolume": 200,
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
    assert selection.candidates[0].quote_volume == 20_000_000
