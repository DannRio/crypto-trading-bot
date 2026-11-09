import pandas as pd
import pytest

from trading_bot.assisted import (
    ExchangeCredentials,
    IntentStatus,
    PreparedOrder,
    TradeIntent,
)
from trading_bot.execution import (
    CCXTOrderGateway,
    KillSwitchService,
    LiveExecutionGuard,
    ManagedLiveBrokerAdapter,
    ManagedOrderStatus,
    OrderStore,
    ReconciliationService,
    client_order_id_for_intent,
    normalize_ccxt_order,
)


class StaticCredentialProvider:
    def load(self, exchange_id):
        return ExchangeCredentials(
            api_key="key",
            secret="secret",
            password="passphrase",
        )


class FakeLiveExchange:
    def __init__(self, config=None):
        self.config = config or {}
        self.has = {
            "createOrder": True,
            "fetchOrder": True,
            "fetchOpenOrders": True,
            "fetchOrders": True,
            "fetchClosedOrders": True,
            "fetchCanceledOrders": True,
            "cancelOrder": True,
        }
        self.orders = {}
        self.create_calls = 0
        self.cancel_calls = 0
        self.required_checked = False

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

    def feature_value(self, symbol, method, feature):
        assert method == "createOrder"
        return feature in {
            "stopLoss",
            "takeProfit",
            "clientOrderId",
        }

    def create_order(
        self,
        symbol,
        order_type,
        side,
        amount,
        price,
        params,
    ):
        self.create_calls += 1
        order_id = f"live-{self.create_calls}"
        raw = {
            "id": order_id,
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "status": "open",
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "remaining": amount,
            "average": None,
            "cost": 0.0,
            "params": dict(params),
        }
        self.orders[order_id] = raw
        return dict(raw)

    def fetch_order(self, order_id, symbol=None):
        raw = self.orders.get(order_id)
        if raw is None:
            raise KeyError(order_id)
        return dict(raw)

    def fetch_open_orders(
        self,
        symbol=None,
        since=None,
        limit=None,
    ):
        return [
            dict(raw)
            for raw in self.orders.values()
            if raw["status"] == "open"
            and (
                symbol is None
                or raw["symbol"] == symbol
            )
        ]

    def fetch_orders(
        self,
        symbol=None,
        since=None,
        limit=None,
    ):
        return [
            dict(raw)
            for raw in self.orders.values()
            if symbol is None
            or raw["symbol"] == symbol
        ]

    def fetch_closed_orders(
        self,
        symbol=None,
        since=None,
        limit=None,
    ):
        return [
            dict(raw)
            for raw in self.orders.values()
            if raw["status"] == "closed"
            and (
                symbol is None
                or raw["symbol"] == symbol
            )
        ]

    def fetch_canceled_orders(
        self,
        symbol=None,
        since=None,
        limit=None,
    ):
        return [
            dict(raw)
            for raw in self.orders.values()
            if raw["status"] == "canceled"
            and (
                symbol is None
                or raw["symbol"] == symbol
            )
        ]

    def cancel_order(self, order_id, symbol=None):
        self.cancel_calls += 1
        raw = self.orders[order_id]
        raw["status"] = "canceled"
        raw["remaining"] = max(
            0.0,
            raw["amount"] - raw["filled"],
        )
        return dict(raw)

    def set_fill(
        self,
        order_id,
        *,
        filled,
        average,
        closed=False,
    ):
        raw = self.orders[order_id]
        raw["filled"] = filled
        raw["average"] = average
        raw["cost"] = filled * average
        raw["remaining"] = max(
            0.0,
            raw["amount"] - filled,
        )
        raw["status"] = (
            "closed"
            if closed
            else "open"
        )

    def add_orphan(self, client_order_id):
        self.orders["orphan-1"] = {
            "id": "orphan-1",
            "clientOrderId": client_order_id,
            "symbol": "BTC/USDT",
            "status": "open",
            "type": "limit",
            "side": "buy",
            "amount": 0.5,
            "price": 90.0,
            "filled": 0.0,
            "remaining": 0.5,
            "average": None,
            "cost": 0.0,
        }


def approved_intent():
    return TradeIntent(
        intent_id="intent-live-1",
        fingerprint="fingerprint",
        plan_hash="plan-hash",
        created_at=pd.Timestamp(
            "2026-11-09T12:00:00Z"
        ),
        signal_time=pd.Timestamp(
            "2026-11-09T11:00:00Z"
        ),
        expires_at=pd.Timestamp(
            "2026-11-10T13:00:00Z"
        ),
        exchange_id="kucoin",
        market_type="spot",
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="trend_continuation",
        signal="LONG",
        score=82,
        status=IntentStatus.APPROVED,
        plan={
            "position_size": 1.23456,
            "reference_entry": 100.126,
            "stop_loss": 95.0,
            "take_profit_2": 110.0,
        },
    )


def test_ccxt_order_normalization_handles_partial_and_closed():
    partial = normalize_ccxt_order(
        {
            "id": "1",
            "clientOrderId": "tb-abc",
            "symbol": "BTC/USDT",
            "status": "open",
            "amount": 1.0,
            "filled": 0.4,
            "average": 100.5,
        }
    )
    assert partial.status == ManagedOrderStatus.PARTIALLY_FILLED
    assert partial.filled_amount == pytest.approx(0.4)
    assert partial.symbol == "BTC/USDT"

    closed = normalize_ccxt_order(
        {
            "id": "1",
            "clientOrderId": "tb-abc",
            "symbol": "BTC/USDT",
            "status": "closed",
            "amount": 1.0,
            "filled": 1.0,
            "average": 100.7,
        }
    )
    assert closed.status == ManagedOrderStatus.FILLED


def test_live_gateway_requires_environment_and_persistent_arm(
    monkeypatch,
    tmp_path,
):
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    exchange = FakeLiveExchange()
    gateway = CCXTOrderGateway(
        exchange,
        guard=guard,
    )
    prepared = PreparedOrder(
        intent_id="intent-1",
        exchange_id="kucoin",
        market_type="spot",
        symbol="BTC/USDT",
        order_type="limit",
        side="buy",
        amount=1.0,
        price=100.0,
        client_order_id=client_order_id_for_intent(
            "intent-1"
        ),
    )

    monkeypatch.delenv(
        "TRADING_LIVE_EXECUTION",
        raising=False,
    )
    with pytest.raises(
        RuntimeError,
        match="disabled",
    ):
        gateway.submit(prepared)

    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    with pytest.raises(
        RuntimeError,
        match="not armed",
    ):
        gateway.submit(prepared)

    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    snapshot = gateway.submit(prepared)
    assert snapshot.status == ManagedOrderStatus.SUBMITTED
    assert exchange.create_calls == 1
    store.close()


def test_managed_live_adapter_is_idempotent_and_attaches_protection(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    exchange = FakeLiveExchange()

    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: exchange,
        now_fn=lambda: pd.Timestamp(
            "2026-11-09T12:05:00Z"
        ),
    )

    first = broker.execute(
        approved_intent()
    )
    second = broker.execute(
        approved_intent()
    )

    assert first.mode == "LIVE"
    assert first.external_order_id == second.external_order_id
    assert exchange.create_calls == 1

    raw = exchange.orders[
        first.external_order_id
    ]
    assert raw["clientOrderId"].startswith("tb-")
    assert raw["params"]["stopLoss"] == {
        "triggerPrice": 95.0
    }
    assert raw["params"]["takeProfit"] == {
        "triggerPrice": 110.0
    }

    managed = store.get_by_intent(
        "intent-live-1"
    )
    assert managed is not None
    assert managed.params["stopLoss"][
        "triggerPrice"
    ] == 95.0
    store.close()


def test_partial_fill_reconciliation_updates_local_state(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    exchange = FakeLiveExchange()
    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: exchange,
    )
    receipt = broker.execute(
        approved_intent()
    )

    exchange.set_fill(
        receipt.external_order_id,
        filled=0.5,
        average=100.25,
    )

    reconciler = ReconciliationService(
        store,
        broker.manager,
        broker.gateway,
        guard,
    )
    report = reconciler.reconcile_all()

    assert report.updated == 1
    managed = store.get_by_intent(
        "intent-live-1"
    )
    assert managed.status == ManagedOrderStatus.PARTIALLY_FILLED
    assert managed.filled_amount == pytest.approx(0.5)
    assert managed.average_price == pytest.approx(100.25)
    store.close()


def test_orphaned_bot_order_latches_halt(
    tmp_path,
):
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    exchange = FakeLiveExchange()
    gateway = CCXTOrderGateway(
        exchange,
        guard=guard,
    )
    from trading_bot.execution import OrderManager

    manager = OrderManager(
        store,
        gateway,
    )
    exchange.add_orphan(
        client_order_id_for_intent(
            "orphan-intent"
        )
    )

    report = ReconciliationService(
        store,
        manager,
        gateway,
        guard,
    ).reconcile_all(
        cancel_orphans=True,
    )

    assert report.orphaned_exchange_orders == 1
    assert exchange.cancel_calls == 1
    state = guard.status()
    assert state.halted is True
    assert state.armed is False
    store.close()


def test_kill_switch_halts_first_and_cancels_open_orders(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    exchange = FakeLiveExchange()
    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: exchange,
    )
    broker.execute(
        approved_intent()
    )

    report = KillSwitchService(
        store,
        broker.manager,
        guard,
    ).trip(
        actor="Danny",
        reason="manual emergency stop",
    )

    assert report.halted is True
    assert report.cancelled_orders == 1
    state = guard.status()
    assert state.halted is True
    assert state.armed is False

    order = store.get_by_intent(
        "intent-live-1"
    )
    assert order.status == ManagedOrderStatus.CANCELLED

    with pytest.raises(
        RuntimeError,
        match="halted",
    ):
        broker.execute(
            TradeIntent(
                **{
                    **approved_intent().__dict__,
                    "intent_id": "intent-live-2",
                }
            )
        )
    store.close()


class NoProtectionExchange(FakeLiveExchange):
    def feature_value(self, symbol, method, feature):
        if feature in {"stopLoss", "takeProfit"}:
            return False
        return super().feature_value(
            symbol,
            method,
            feature,
        )


def test_live_adapter_refuses_unprotected_entry(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )

    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: NoProtectionExchange(
            config
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="stop-loss",
    ):
        broker.execute(
            approved_intent()
        )
    store.close()



class TimeoutAfterAcceptExchange(FakeLiveExchange):
    def __init__(self, config=None):
        super().__init__(config)
        self.raise_after_accept = True

    def create_order(
        self,
        symbol,
        order_type,
        side,
        amount,
        price,
        params,
    ):
        raw = super().create_order(
            symbol,
            order_type,
            side,
            amount,
            price,
            params,
        )
        if self.raise_after_accept:
            self.raise_after_accept = False
            raise TimeoutError(
                "Timed out after exchange accepted order."
            )
        return raw


def test_live_timeout_reconciles_original_order_without_duplicate(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    exchange = TimeoutAfterAcceptExchange()
    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: exchange,
        now_fn=lambda: pd.Timestamp(
            "2026-11-09T12:05:00Z"
        ),
    )

    with pytest.raises(TimeoutError):
        broker.execute(
            approved_intent()
        )

    local = store.get_by_intent(
        "intent-live-1"
    )
    assert local is not None
    assert local.status == ManagedOrderStatus.UNKNOWN
    assert exchange.create_calls == 1

    report = ReconciliationService(
        store,
        broker.manager,
        broker.gateway,
        guard,
    ).reconcile_all()

    assert report.updated == 1
    reconciled = store.get_by_intent(
        "intent-live-1"
    )
    assert reconciled.status == ManagedOrderStatus.SUBMITTED
    assert reconciled.external_order_id == "live-1"

    with pytest.raises(
        ValueError,
        match="APPROVED",
    ):
        broker.execute(
            TradeIntent(
                **{
                    **approved_intent().__dict__,
                    "status": IntentStatus.EXECUTED,
                }
            )
        )
    assert exchange.create_calls == 1
    store.close()


def test_execution_halt_persists_across_restart_and_requires_reset(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    path = tmp_path / "orders.db"

    first = OrderStore(path)
    first_guard = LiveExecutionGuard(
        first
    )
    first_guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    first_guard.trip(
        actor="Danny",
        reason="test halt",
    )
    first.close()

    restored = OrderStore(path)
    guard = LiveExecutionGuard(restored)
    state = guard.status()
    assert state.halted is True
    assert state.armed is False
    assert state.halt_reason == "test halt"

    with pytest.raises(
        RuntimeError,
        match="Reset the halt",
    ):
        guard.arm(
            actor="Danny",
            confirmation="ARM_LIVE",
        )

    guard.reset_halt(
        actor="Danny",
        confirmation="RESET_HALT",
    )
    reset = guard.status()
    assert reset.halted is False
    assert reset.armed is False
    restored.close()


class NoClientOrderIdExchange(FakeLiveExchange):
    def feature_value(self, symbol, method, feature):
        if feature == "clientOrderId":
            return False
        return super().feature_value(
            symbol,
            method,
            feature,
        )


def test_live_adapter_refuses_market_without_client_order_id(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "TRADING_LIVE_EXECUTION",
        "ENABLED",
    )
    store = OrderStore(tmp_path / "orders.db")
    guard = LiveExecutionGuard(store)
    guard.arm(
        actor="Danny",
        confirmation="ARM_LIVE",
    )
    exchange = NoClientOrderIdExchange()

    broker = ManagedLiveBrokerAdapter(
        exchange_id="kucoin",
        market_type="spot",
        store=store,
        guard=guard,
        credential_provider=StaticCredentialProvider(),
        exchange_factory=lambda config: exchange,
    )

    with pytest.raises(
        RuntimeError,
        match="clientOrderId",
    ):
        broker.execute(
            approved_intent()
        )
    assert exchange.create_calls == 0
    store.close()
