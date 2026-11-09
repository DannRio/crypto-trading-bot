"""CCXT public market data with explicit instrument-type verification.

Kraken spot and Kraken Futures are separate CCXT exchanges. Do not rely on
options.defaultType to turn a spot exchange into a derivatives exchange.
"""
import ccxt


MARKET_TYPES = {"spot", "swap", "future"}


def validate_exchange_selection(exchange_id: str, market_type: str) -> None:
    if market_type not in MARKET_TYPES:
        raise ValueError(f"Unknown market type {market_type!r}; choose spot, swap or future.")
    if exchange_id == "kraken" and market_type != "spot":
        raise ValueError(
            "CCXT 'kraken' is Kraken spot. For Kraken perpetuals use "
            "exchange='krakenfutures', market_type='swap' and a market symbol "
            "from the exchange's markets list."
        )
    if exchange_id == "krakenfutures" and market_type == "spot":
        raise ValueError(
            "CCXT 'krakenfutures' has derivatives, not spot. "
            "Use exchange='kraken' for spot trading."
        )


class CCXTClient:
    def __init__(
        self,
        exchange_id: str = "kraken",
        market_type: str = "spot",
    ):
        validate_exchange_selection(exchange_id, market_type)
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.exchange = self._create_exchange()
        self._validated: dict[str, dict] = {}

    def _create_exchange(self):
        if not hasattr(ccxt, self.exchange_id):
            raise ValueError(f"Unsupported CCXT exchange: {self.exchange_id}")
        exchange_class = getattr(ccxt, self.exchange_id)
        return exchange_class({
            "enableRateLimit": True,
            "options": {"defaultType": self.market_type},
        })

    def available_markets(self, search: str = "", limit: int = 30) -> list[dict]:
        """Return only symbols whose *actual* CCXT market type matches."""
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100.")
        markets = self.exchange.load_markets()
        text = search.upper().strip()
        matching = []
        for symbol, market in markets.items():
            if not self._type_matches(market):
                continue
            if market.get("active") is False:
                continue
            if text and text not in symbol.upper():
                continue
            matching.append({
                "symbol": symbol,
                "type": self.market_type,
                "base": market.get("base"),
                "quote": market.get("quote"),
                "settle": market.get("settle"),
                "linear": market.get("linear"),
                "contract_size": market.get("contractSize"),
            })
            if len(matching) >= limit:
                break
        return matching

    def _type_matches(self, market: dict) -> bool:
        # Actual CCXT market metadata is authoritative, not the symbol suffix.
        return bool(market.get(self.market_type)) or (
            market.get("type") == self.market_type
        )

    def validate_market(self, symbol: str) -> dict:
        if symbol in self._validated:
            return self._validated[symbol]
        markets = self.exchange.load_markets()
        market = markets.get(symbol)
        if market is None:
            examples = [m["symbol"] for m in self.available_markets(
                search=symbol.split("/", 1)[0], limit=5)]
            hint = f" Try: {', '.join(examples)}." if examples else ""
            raise ValueError(
                f"{symbol!r} is not listed on {self.exchange_id}. "
                f"Select a symbol from its actual market list.{hint}"
            )
        if not self._type_matches(market):
            actual = market.get("type", "unknown")
            raise ValueError(
                f"{symbol} on {self.exchange_id} is a {actual} market, "
                f"not the requested {self.market_type}. "
                "Select the correct exchange, instrument type and symbol."
            )
        if market.get("active") is False:
            raise ValueError(f"{symbol} is inactive on {self.exchange_id}.")
        if self.market_type != "spot" and not market.get("contract"):
            raise ValueError(
                f"{symbol} is not a contract market according to CCXT metadata."
            )
        self._validated[symbol] = market
        return market

    def fetch_ticker(self, symbol: str):
        self.validate_market(symbol)
        return self.exchange.fetch_ticker(symbol)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: int | None = None,
        params: dict | None = None,
    ):
        self.validate_market(symbol)
        if self.exchange.has.get("fetchOHLCV") is False:
            raise ValueError(
                f"{self.exchange_id} does not advertise OHLCV support."
            )
        if self.exchange.timeframes and timeframe not in self.exchange.timeframes:
            raise ValueError(
                f"Timeframe {timeframe} unsupported on {self.exchange_id}. "
                f"Supported: {', '.join(self.exchange.timeframes)}"
            )
        return self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
            params=params or {},
        )
