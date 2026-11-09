from __future__ import annotations

import math

from trading_bot.assisted.models import PreparedOrder
from trading_bot.execution.control import LiveExecutionGuard
from trading_bot.execution.gateway import OrderGateway
from trading_bot.execution.models import (
    ExchangeOrderSnapshot,
    ManagedOrderStatus,
)


def _finite_float(
    value,
    default: float | None = None,
) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return (
        number
        if math.isfinite(number)
        else default
    )


def normalize_ccxt_order(
    order: dict,
    *,
    fallback_client_order_id: str | None = None,
) -> ExchangeOrderSnapshot:
    if not isinstance(order, dict):
        raise ValueError(
            "CCXT order response must be a dictionary."
        )

    external_order_id = order.get("id")
    if not external_order_id:
        raise ValueError(
            "CCXT order response has no order id."
        )

    client_order_id = (
        order.get("clientOrderId")
        or fallback_client_order_id
    )
    status_raw = str(
        order.get("status") or ""
    ).lower()
    filled = max(
        0.0,
        _finite_float(
            order.get("filled"),
            0.0,
        )
        or 0.0,
    )
    amount = _finite_float(order.get("amount"))
    remaining = _finite_float(
        order.get("remaining")
    )

    if status_raw == "open":
        status = (
            ManagedOrderStatus.PARTIALLY_FILLED
            if filled > 0
            else ManagedOrderStatus.SUBMITTED
        )
    elif status_raw == "closed":
        status = ManagedOrderStatus.FILLED
    elif status_raw in {
        "canceled",
        "cancelled",
        "expired",
    }:
        status = ManagedOrderStatus.CANCELLED
    elif status_raw == "rejected":
        status = ManagedOrderStatus.REJECTED
    else:
        if (
            amount is not None
            and amount > 0
            and filled >= amount
        ):
            status = ManagedOrderStatus.FILLED
        elif filled > 0:
            status = ManagedOrderStatus.PARTIALLY_FILLED
        elif remaining is not None and remaining > 0:
            status = ManagedOrderStatus.SUBMITTED
        else:
            status = ManagedOrderStatus.UNKNOWN

    average = _finite_float(
        order.get("average")
    )
    if average is None and filled > 0:
        cost = _finite_float(order.get("cost"))
        if cost is not None and cost >= 0:
            average = cost / filled

    return ExchangeOrderSnapshot(
        external_order_id=str(
            external_order_id
        ),
        client_order_id=(
            str(client_order_id)
            if client_order_id is not None
            else None
        ),
        status=status,
        filled_amount=filled,
        average_price=average,
        symbol=(
            str(order.get("symbol"))
            if order.get("symbol")
            else None
        ),
        message=(
            "Normalized from CCXT unified order "
            f"status={status_raw or 'unknown'}."
        ),
    )


class CCXTOrderGateway(OrderGateway):
    """CCXT-backed order gateway.

    Read/reconcile/cancel operations are available without an armed submit
    gate. New order submission always requires LiveExecutionGuard approval.
    """

    def __init__(
        self,
        exchange,
        *,
        guard: LiveExecutionGuard | None = None,
        lookup_limit: int = 100,
    ):
        if lookup_limit <= 0:
            raise ValueError(
                "lookup_limit must be positive."
            )
        self.exchange = exchange
        self.guard = guard
        self.lookup_limit = lookup_limit

    def _supports(
        self,
        capability: str,
    ) -> bool:
        has = getattr(
            self.exchange,
            "has",
            {},
        ) or {}
        return bool(has.get(capability))

    def submit(
        self,
        order: PreparedOrder,
    ) -> ExchangeOrderSnapshot:
        if self.guard is None:
            raise RuntimeError(
                "Live order submission has no execution guard."
            )
        self.guard.assert_can_submit()

        if not self._supports("createOrder"):
            raise NotImplementedError(
                "Exchange does not support createOrder."
            )

        params = dict(order.params or {})
        configured_client_id = params.get(
            "clientOrderId"
        )
        if (
            configured_client_id is not None
            and str(configured_client_id)
            != order.client_order_id
        ):
            raise ValueError(
                "Prepared order params contain a conflicting "
                "clientOrderId."
            )
        params["clientOrderId"] = (
            order.client_order_id
        )

        raw = self.exchange.create_order(
            order.symbol,
            order.order_type,
            order.side,
            order.amount,
            order.price,
            params,
        )
        return normalize_ccxt_order(
            raw,
            fallback_client_order_id=(
                order.client_order_id
            ),
        )

    def _find_raw_by_external_id(
        self,
        external_order_id: str,
        symbol: str,
    ) -> dict | None:
        if self._supports("fetchOrder"):
            try:
                return self.exchange.fetch_order(
                    external_order_id,
                    symbol,
                )
            except Exception:
                # Fall through to list endpoints. This helps on exchanges
                # where fetchOrder exists but cannot retrieve older orders.
                pass

        collections: list[list[dict]] = []
        if self._supports("fetchOpenOrders"):
            collections.append(
                self.exchange.fetch_open_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )
        if self._supports("fetchOrders"):
            collections.append(
                self.exchange.fetch_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )
        elif self._supports("fetchClosedOrders"):
            collections.append(
                self.exchange.fetch_closed_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )

        for orders in collections:
            for raw in orders or []:
                if str(raw.get("id")) == str(
                    external_order_id
                ):
                    return raw
        return None

    def fetch(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        raw = self._find_raw_by_external_id(
            external_order_id,
            symbol,
        )
        if raw is None:
            return None
        return normalize_ccxt_order(raw)

    def _candidate_orders(
        self,
        symbol: str,
    ) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()

        def extend(items) -> None:
            for raw in items or []:
                order_id = str(
                    raw.get("id") or ""
                )
                dedupe_key = order_id or str(
                    raw.get("clientOrderId")
                    or id(raw)
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                result.append(raw)

        if self._supports("fetchOpenOrders"):
            extend(
                self.exchange.fetch_open_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )
        if self._supports("fetchOrders"):
            extend(
                self.exchange.fetch_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )
        else:
            if self._supports(
                "fetchClosedOrders"
            ):
                extend(
                    self.exchange.fetch_closed_orders(
                        symbol,
                        None,
                        self.lookup_limit,
                    )
                )
            if self._supports(
                "fetchCanceledOrders"
            ):
                extend(
                    self.exchange.fetch_canceled_orders(
                        symbol,
                        None,
                        self.lookup_limit,
                    )
                )

        return result

    def find_by_client_order_id(
        self,
        client_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot | None:
        for raw in self._candidate_orders(symbol):
            if str(
                raw.get("clientOrderId") or ""
            ) == client_order_id:
                return normalize_ccxt_order(
                    raw,
                    fallback_client_order_id=(
                        client_order_id
                    ),
                )
        return None

    def list_open_orders(
        self,
        symbol: str | None = None,
    ) -> list[ExchangeOrderSnapshot]:
        if self._supports("fetchOpenOrders"):
            raw_orders = (
                self.exchange.fetch_open_orders(
                    symbol,
                    None,
                    self.lookup_limit,
                )
            )
        elif self._supports("fetchOrders"):
            raw_orders = [
                raw
                for raw in (
                    self.exchange.fetch_orders(
                        symbol,
                        None,
                        self.lookup_limit,
                    )
                    or []
                )
                if str(
                    raw.get("status") or ""
                ).lower()
                == "open"
            ]
        else:
            raise NotImplementedError(
                "Exchange exposes neither fetchOpenOrders "
                "nor fetchOrders."
            )

        return [
            normalize_ccxt_order(raw)
            for raw in raw_orders or []
        ]

    def cancel(
        self,
        external_order_id: str,
        symbol: str,
    ) -> ExchangeOrderSnapshot:
        if not self._supports("cancelOrder"):
            raise NotImplementedError(
                "Exchange does not support cancelOrder."
            )

        raw = self.exchange.cancel_order(
            external_order_id,
            symbol,
        )
        if (
            isinstance(raw, dict)
            and raw.get("id")
        ):
            return normalize_ccxt_order(raw)

        refreshed = self.fetch(
            external_order_id,
            symbol,
        )
        if refreshed is not None:
            return refreshed

        return ExchangeOrderSnapshot(
            external_order_id=str(
                external_order_id
            ),
            client_order_id=None,
            status=ManagedOrderStatus.CANCELLED,
            filled_amount=0.0,
            average_price=None,
            symbol=symbol,
            message=(
                "Exchange acknowledged cancellation but did not "
                "return a queryable unified order."
            ),
        )
