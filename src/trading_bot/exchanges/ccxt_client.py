import ccxt


class CCXTClient:
    def __init__(
        self,
        exchange_id: str = "kraken",
        market_type: str = "spot",
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
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
                "options": {
                    "defaultType": self.market_type,
                },
            }
        )

    def supports(self, capability: str) -> bool:
        return bool(self.exchange.has.get(capability))

    def load_markets(self, reload: bool = False):
        return self.exchange.load_markets(reload)

    def fetch_ticker(self, symbol: str):
        return self.exchange.fetch_ticker(symbol)

    def fetch_tickers(
        self,
        symbols: list[str] | None = None,
    ):
        if not self.supports("fetchTickers"):
            raise NotImplementedError(
                f"{self.exchange_id} does not support fetchTickers."
            )
        return self.exchange.fetch_tickers(symbols)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: int | None = None,
        params: dict | None = None,
    ):
        return self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
            params=params or {},
        )
