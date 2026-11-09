import os

import pandas as pd
import pytest

from trading_bot.assisted import (
    AuthenticatedCCXTBrokerAdapter,
    EnvironmentCredentialProvider,
    ExchangeCredentials,
    IntentStatus,
    TradeIntent,
)


class StaticCredentialProvider:
    def load(self, exchange_id):
        return ExchangeCredentials(
            api_key="dummy-key",
            secret="dummy-secret",
            password="dummy-passphrase",
        )


class FakeExchange:
    def __init__(self, config):
        self.config = config
        self.required_checked = False
        self.balance_probed = False

    def check_required_credentials(self):
        self.required_checked = True

    def load_markets(self, reload=False):
        return {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "active": True,
                "limits": {
                    "amount": {
                        "min": 0.001,
                        "max": 100.0,
                    },
                    "price": {
                        "min": 1.0,
                        "max": 1_000_000.0,
                    },
                    "cost": {
                        "min": 10.0,
                        "max": 10_000_000.0,
                    },
                },
            }
        }

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.3f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    def fetch_balance(self):
        self.balance_probed = True
        return {"total": {"USDT": 1000.0}}


def intent(
    *,
    signal="LONG",
    market_type="spot",
    amount=1.23456,
    price=100.126,
):
    return TradeIntent(
        intent_id="intent-1",
        fingerprint="fingerprint",
        plan_hash="plan-hash",
        created_at=pd.Timestamp("2026-01-01T10:05:00Z"),
        signal_time=pd.Timestamp("2026-01-01T10:00:00Z"),
        expires_at=pd.Timestamp("2026-01-01T11:00:00Z"),
        exchange_id="kucoin",
        market_type=market_type,
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="trend_continuation",
        signal=signal,
        score=82,
        status=IntentStatus.APPROVED,
        plan={
            "position_size": amount,
            "reference_entry": price,
        },
    )


def test_exchange_credentials_do_not_leak_in_repr():
    credentials = ExchangeCredentials(
        api_key="visible-key",
        secret="super-secret",
        password="passphrase",
        uid="uid-1",
    )

    rendered = repr(credentials)
    assert "super-secret" not in rendered
    assert "passphrase" not in rendered
    assert "visible-key" not in rendered


def test_environment_provider_requires_key_and_secret(monkeypatch):
    provider = EnvironmentCredentialProvider()
    names = provider.variable_names("kucoin")

    for name in names.values():
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match=names["api_key"]):
        provider.load("kucoin")

    monkeypatch.setenv(names["api_key"], "key")
    monkeypatch.setenv(names["secret"], "secret")
    monkeypatch.setenv(names["password"], "pass")

    credentials = provider.load("kucoin")
    assert credentials.api_key == "key"
    assert credentials.secret == "secret"
    assert credentials.password == "pass"


def test_authenticated_adapter_prepares_precision_checked_order():
    captured = {}

    def factory(config):
        captured["config"] = config
        exchange = FakeExchange(config)
        captured["exchange"] = exchange
        return exchange

    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=factory,
    )

    prepared = adapter.prepare_order(intent())

    assert captured["exchange"].required_checked is True
    assert captured["config"]["apiKey"] == "dummy-key"
    assert captured["config"]["secret"] == "dummy-secret"
    assert prepared.side == "buy"
    assert prepared.order_type == "limit"
    assert prepared.amount == pytest.approx(1.235)
    assert prepared.price == pytest.approx(100.13)
    assert prepared.client_order_id.startswith("tb-")
    assert prepared.client_order_id != "intent-1"
    assert adapter.capabilities.live_execution is False


def test_authenticated_adapter_rejects_exchange_limit_violation():
    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=FakeExchange,
    )

    with pytest.raises(ValueError, match="below exchange minimum"):
        adapter.prepare_order(
            intent(amount=0.0001)
        )


def test_authenticated_adapter_spot_short_is_not_executable():
    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=FakeExchange,
    )

    with pytest.raises(ValueError, match="SHORT"):
        adapter.prepare_order(
            intent(signal="SHORT")
        )


def test_private_access_probe_is_explicit():
    created = {}

    def factory(config):
        exchange = FakeExchange(config)
        created["exchange"] = exchange
        return exchange

    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=factory,
    )

    assert created["exchange"].balance_probed is False
    balance = adapter.validate_private_access()
    assert balance["total"]["USDT"] == 1000.0
    assert created["exchange"].balance_probed is True


def test_live_execution_is_hard_disabled():
    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=FakeExchange,
    )

    with pytest.raises(RuntimeError, match="Live order submission is disabled"):
        adapter.execute(intent())



class ContractFakeExchange(FakeExchange):
    def load_markets(self, reload=False):
        markets = super().load_markets(
            reload
        )
        market = markets["BTC/USDT"]
        market["contract"] = True
        market["contractSize"] = 0.01
        return markets


def test_derivative_base_size_is_converted_to_contracts():
    adapter = AuthenticatedCCXTBrokerAdapter(
        exchange_id="kucoin",
        market_type="swap",
        credential_provider=StaticCredentialProvider(),
        exchange_factory=ContractFakeExchange,
    )

    prepared = adapter.prepare_order(
        intent(
            market_type="swap",
            amount=1.23456,
            price=100.126,
        )
    )

    assert prepared.amount == pytest.approx(
        123.456
    )
    assert prepared.price == pytest.approx(
        100.13
    )
