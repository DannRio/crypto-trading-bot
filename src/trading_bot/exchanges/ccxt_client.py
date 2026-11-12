import ccxt
from threading import RLock
from trading_bot.exchanges.routing import canonical_exchange_id


_PUBLIC_CLIENTS: dict[tuple[str, str], "CCXTClient"] = {}
_CLIENTS_LOCK = RLock()


def get_public_client(exchange_id: str = "kraken", market_type: str = "spot") -> "CCXTClient":
    """Reuse public sessions, market metadata and the venue rate limiter.

    Private/authenticated execution adapters deliberately have separate clients.
    Synchronous calls on one public client are serialized across threads in this process.
    """
    exchange_id = canonical_exchange_id(exchange_id, market_type)
    key = (exchange_id, market_type)
    with _CLIENTS_LOCK:
        if key not in _PUBLIC_CLIENTS:
            _PUBLIC_CLIENTS[key] = CCXTClient(exchange_id, market_type)
        return _PUBLIC_CLIENTS[key]


class CCXTClient:
    def __init__(
        self,
        exchange_id: str = "kraken",
        market_type: str = "spot",
    ):
        self.exchange_id = canonical_exchange_id(exchange_id, market_type)
        self.market_type = market_type
        self._request_lock = RLock()
        self.exchange = self._create_exchange()

    def _create_exchange(self):
        if not hasattr(ccxt, self.exchange_id):
            raise ValueError(
                f"Unsupported CCXT exchange: {self.exchange_id}"
            )

        exchange_class = getattr(ccxt, self.exchange_id)

        return exchange_class(
            {
                "enableRateLimit": True,
                "timeout": 10_000,
                "options": {
                    "defaultType": self.market_type,
                },
            }
        )

    def supports(self, capability: str) -> bool:
        return bool(self.exchange.has.get(capability))

    def load_markets(self, reload: bool = False):
        with self._request_lock:
            return self.exchange.load_markets(reload)

    def fetch_ticker(self, symbol: str):
        with self._request_lock:
            return self.exchange.fetch_ticker(symbol)

    def fetch_currencies(self):
        if not self.supports("fetchCurrencies"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchCurrencies."
            )
        with self._request_lock:
            return self.exchange.fetch_currencies()

    def fetch_order_book(self, symbol: str, limit: int | None = None):
        if not self.supports("fetchOrderBook"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchOrderBook."
            )
        with self._request_lock:
            return self.exchange.fetch_order_book(symbol, limit=limit)

    def fetch_funding_rate(self, symbol: str):
        if not self.supports("fetchFundingRate"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchFundingRate."
            )
        with self._request_lock:
            return self.exchange.fetch_funding_rate(symbol)

    def fetch_open_interest(self, symbol: str):
        if not self.supports("fetchOpenInterest"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchOpenInterest."
            )
        with self._request_lock:
            return self.exchange.fetch_open_interest(symbol)

    def fetch_tickers(
        self,
        symbols: list[str] | None = None,
    ):
        if not self.supports("fetchTickers"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchTickers."
            )
        with self._request_lock:
            return self.exchange.fetch_tickers(symbols)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: int | None = None,
        params: dict | None = None,
    ):
        with self._request_lock:
            return self.exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=since,
                limit=limit,
                params=params or {},
            )
