"""Verify CCXT venue and instrument identification without network access."""
import pytest

from trading_bot.exchanges.ccxt_client import (
    CCXTClient,
    validate_exchange_selection,
)


class FakeExchange:
    def __init__(self, options):
        self.config = options
        self.has = {"fetchOHLCV": True}
        self.timeframes = {"1h": "1h"}
        self.calls = []
        self.markets = {
            "SOL/USDT": {
                "symbol": "SOL/USDT", "type": "spot", "spot": True,
                "swap": False, "future": False, "contract": False,
                "active": True, "base": "SOL", "quote": "USDT",
            },
            "SOL/USDT:USDT": {
                "symbol": "SOL/USDT:USDT", "type": "swap", "spot": False,
                "swap": True, "future": False, "contract": True,
                "active": True, "base": "SOL", "quote": "USDT",
                "settle": "USDT", "linear": True, "contractSize": 1.0,
            },
            "SOL/USDT:USDT-270101": {
                "symbol": "SOL/USDT:USDT-270101", "type": "future",
                "spot": False, "swap": False, "future": True,
                "contract": True, "active": True,
            },
        }

    def load_markets(self):
        return self.markets

    def fetch_ticker(self, symbol):
        return {"symbol": symbol}

    def fetch_ohlcv(self, symbol, timeframe, since, limit, params):
        self.calls.append(symbol)
        return [[1, 100, 101, 99, 100, 100]]


def test_kraken_spot_and_futures_must_be_separate_ccxt_venues():
    with pytest.raises(ValueError, match="krakenfutures"):
        validate_exchange_selection("kraken", "future")
    with pytest.raises(ValueError, match="krakenfutures"):
        validate_exchange_selection("kraken", "swap")
    with pytest.raises(ValueError, match="not spot"):
        validate_exchange_selection("krakenfutures", "spot")
    validate_exchange_selection("krakenfutures", "swap")
    validate_exchange_selection("kraken", "spot")


def test_reject_spot_symbol_when_swap_requested(monkeypatch):
    import ccxt
    monkeypatch.setattr(ccxt, "bybit", FakeExchange)
    client = CCXTClient(exchange_id="bybit", market_type="swap")
    with pytest.raises(ValueError, match="spot market, not the requested swap"):
        client.fetch_ohlcv("SOL/USDT")
    assert client.exchange.calls == []
    result = client.fetch_ohlcv("SOL/USDT:USDT")
    assert len(result) == 1
    assert client.exchange.calls == ["SOL/USDT:USDT"]


def test_market_discovery_filters_actual_type_not_symbol_guess(monkeypatch):
    import ccxt
    monkeypatch.setattr(ccxt, "bybit", FakeExchange)
    client = CCXTClient(exchange_id="bybit", market_type="swap")
    options = client.available_markets(search="SOL")
    assert [item["symbol"] for item in options] == ["SOL/USDT:USDT"]
    assert options[0]["contract_size"] == 1.0
    with pytest.raises(ValueError, match="not listed"):
        client.validate_market("SOL/USD:USD")


def test_dated_future_market_is_not_perpetual(monkeypatch):
    import ccxt
    monkeypatch.setattr(ccxt, "bybit", FakeExchange)
    client = CCXTClient(exchange_id="bybit", market_type="future")
    with pytest.raises(ValueError, match="swap market"):
        client.fetch_ticker("SOL/USDT:USDT")
    assert client.fetch_ticker("SOL/USDT:USDT-270101")["symbol"].startswith("SOL")
