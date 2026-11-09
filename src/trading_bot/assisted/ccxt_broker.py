from collections.abc import Callable

import ccxt

from trading_bot.assisted.broker import BrokerAdapter
from trading_bot.assisted.credentials import (
    EnvironmentCredentialProvider,
    ExchangeCredentials,
)
from trading_bot.assisted.models import (
    BrokerCapabilities,
    ExecutionReceipt,
    PreparedOrder,
    TradeIntent,
)


class AuthenticatedCCXTBrokerAdapter(BrokerAdapter):
    """Authenticated exchange boundary with live order submission disabled.

    This adapter loads credentials, validates CCXT credential requirements,
    loads exchange markets and prepares a precision/limit-checked order.
    It deliberately does not call create_order.
    """

    def __init__(
        self,
        exchange_id: str,
        market_type: str,
        credential_provider: EnvironmentCredentialProvider | None = None,
        exchange_factory: Callable[[dict], object] | None = None,
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.credential_provider = (
            credential_provider
            or EnvironmentCredentialProvider()
        )
        self.credentials = self.credential_provider.load(
            exchange_id
        )
        self.exchange = self._create_exchange(
            exchange_factory
        )
        self._markets: dict | None = None

        self._capabilities = BrokerCapabilities(
            broker_id=f"ccxt:{exchange_id}",
            supported_market_types=(market_type,),
            supports_long=True,
            supports_short=market_type != "spot",
            live_execution=False,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def _exchange_config(
        self,
        credentials: ExchangeCredentials,
    ) -> dict:
        config = {
            "enableRateLimit": True,
            "apiKey": credentials.api_key,
            "secret": credentials.secret,
            "options": {
                "defaultType": self.market_type,
            },
        }
        if credentials.password is not None:
            config["password"] = credentials.password
        if credentials.uid is not None:
            config["uid"] = credentials.uid
        return config

    def _create_exchange(
        self,
        exchange_factory: Callable[[dict], object] | None,
    ):
        config = self._exchange_config(
            self.credentials
        )
        if exchange_factory is not None:
            exchange = exchange_factory(config)
        else:
            if not hasattr(ccxt, self.exchange_id):
                raise ValueError(
                    f"Unsupported CCXT exchange: {self.exchange_id}"
                )
            exchange_class = getattr(
                ccxt,
                self.exchange_id,
            )
            exchange = exchange_class(config)

        check = getattr(
            exchange,
            "check_required_credentials",
            None,
        )
        if callable(check):
            check()
        return exchange

    def load_markets(self, reload: bool = False) -> dict:
        markets = self.exchange.load_markets(reload)
        if not isinstance(markets, dict):
            raise RuntimeError(
                "Exchange returned invalid market metadata."
            )
        self._markets = markets
        return markets

    def validate_private_access(self):
        """Explicitly perform a private read-only credential probe."""
        fetch_balance = getattr(
            self.exchange,
            "fetch_balance",
            None,
        )
        if not callable(fetch_balance):
            raise NotImplementedError(
                f"{self.exchange_id} does not expose fetch_balance "
                "through this CCXT adapter."
            )
        return fetch_balance()

    @staticmethod
    def _limit(
        market: dict,
        category: str,
        boundary: str,
    ) -> float | None:
        value = (
            market.get("limits", {})
            .get(category, {})
            .get(boundary)
        )
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _check_range(
        name: str,
        value: float,
        minimum: float | None,
        maximum: float | None,
    ) -> None:
        if minimum is not None and value < minimum:
            raise ValueError(
                f"{name} {value} is below exchange minimum {minimum}."
            )
        if maximum is not None and value > maximum:
            raise ValueError(
                f"{name} {value} exceeds exchange maximum {maximum}."
            )

    def prepare_order(
        self,
        intent: TradeIntent,
    ) -> PreparedOrder:
        super().validate_intent(intent)

        if intent.exchange_id != self.exchange_id:
            raise ValueError(
                "Intent exchange does not match authenticated adapter."
            )

        markets = (
            self._markets
            if self._markets is not None
            else self.load_markets()
        )
        market = markets.get(intent.symbol)
        if market is None:
            raise ValueError(
                f"Exchange market is unavailable: {intent.symbol}"
            )
        if market.get("active") is False:
            raise ValueError(
                f"Exchange market is inactive: {intent.symbol}"
            )

        requested_amount = float(
            intent.plan["position_size"]
        )
        requested_price = float(
            intent.plan["reference_entry"]
        )
        if requested_amount <= 0 or requested_price <= 0:
            raise ValueError(
                "Intent amount and price must be positive."
            )

        amount = float(
            self.exchange.amount_to_precision(
                intent.symbol,
                requested_amount,
            )
        )
        price = float(
            self.exchange.price_to_precision(
                intent.symbol,
                requested_price,
            )
        )
        cost = amount * price

        self._check_range(
            "amount",
            amount,
            self._limit(market, "amount", "min"),
            self._limit(market, "amount", "max"),
        )
        self._check_range(
            "price",
            price,
            self._limit(market, "price", "min"),
            self._limit(market, "price", "max"),
        )
        self._check_range(
            "cost",
            cost,
            self._limit(market, "cost", "min"),
            self._limit(market, "cost", "max"),
        )

        return PreparedOrder(
            intent_id=intent.intent_id,
            exchange_id=intent.exchange_id,
            market_type=intent.market_type,
            symbol=intent.symbol,
            order_type="limit",
            side=(
                "buy"
                if intent.signal == "LONG"
                else "sell"
            ),
            amount=amount,
            price=price,
            client_order_id=intent.intent_id,
        )

    def execute(
        self,
        intent: TradeIntent,
    ) -> ExecutionReceipt:
        self.prepare_order(intent)
        raise RuntimeError(
            "Authenticated broker is preparation-only. "
            "Live order submission is disabled until order management, "
            "idempotency, reconciliation and kill-switch controls exist."
        )
