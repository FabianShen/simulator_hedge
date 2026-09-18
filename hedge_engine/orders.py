"""Pure ownership registry for order intents and broker order IDs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from hedge_engine.accounting import STRATEGIES


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    account_id: str
    strategy: str
    instrument: str
    quantity: int
    offset: str
    order_type: str
    limit_price: Decimal | None
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.account_id or not self.instrument:
            raise ValueError(
                "client_order_id, account_id, and instrument must not be empty"
            )
        if self.strategy not in STRATEGIES:
            raise ValueError("order strategy must be ALPHA or BETA")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ValueError("order quantity must be a signed integer")
        if self.quantity == 0:
            raise ValueError("order quantity must not be zero")
        if self.offset not in {"OPEN", "CLOSE"}:
            raise ValueError("order offset must be OPEN or CLOSE")
        if self.order_type not in {"LIMIT", "COUNTERPARTY", "LAST", "MARKET"}:
            raise ValueError("unsupported order_type")
        if self.order_type == "LIMIT" and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError("LIMIT order requires a positive limit_price")
        if self.order_type != "LIMIT" and self.limit_price is not None:
            raise ValueError("non-LIMIT order must not have limit_price")
        if self.created_at.tzinfo is None:
            raise ValueError("order created_at must be timezone-aware")


@dataclass(frozen=True)
class OrderRegistry:
    account_id: str
    revision: int
    intents: Mapping[str, OrderIntent]
    broker_orders: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("registry account_id must not be empty")
        if self.revision < 0:
            raise ValueError("registry revision must not be negative")
        for client_order_id, intent in self.intents.items():
            if (
                client_order_id != intent.client_order_id
                or intent.account_id != self.account_id
            ):
                raise ValueError("order intent does not belong to this registry")
        clients = list(self.broker_orders.values())
        if len(clients) != len(set(clients)):
            raise ValueError("one client_order_id cannot bind multiple broker orders")
        if not set(clients) <= set(self.intents):
            raise ValueError("broker order binding references an unknown intent")

    @property
    def order_strategies(self) -> dict[str, str]:
        return {
            order_id: self.intents[client_id].strategy
            for order_id, client_id in self.broker_orders.items()
        }


def empty_order_registry(account_id: str) -> OrderRegistry:
    return OrderRegistry(account_id, 0, {}, {})


def register_order_intent(
    registry: OrderRegistry, intent: OrderIntent
) -> OrderRegistry:
    """Persist an intent before submission; exact retries are idempotent."""

    if intent.account_id != registry.account_id:
        raise ValueError("order intent and registry account IDs do not match")
    previous = registry.intents.get(intent.client_order_id)
    if previous is not None:
        if previous != intent:
            raise ValueError(
                f"client_order_id {intent.client_order_id} has conflicting contents"
            )
        return registry
    intents = dict(registry.intents)
    intents[intent.client_order_id] = intent
    return OrderRegistry(
        registry.account_id,
        registry.revision + 1,
        intents,
        dict(registry.broker_orders),
    )


def bind_broker_order(
    registry: OrderRegistry, *, client_order_id: str, order_id: str
) -> OrderRegistry:
    """Bind the simulator order ID returned for one previously saved intent."""

    if client_order_id not in registry.intents:
        raise ValueError(f"unknown client_order_id: {client_order_id}")
    if not order_id:
        raise ValueError("order_id must not be empty")
    existing_client = registry.broker_orders.get(order_id)
    if existing_client is not None:
        if existing_client != client_order_id:
            raise ValueError(f"order_id {order_id} is already bound")
        return registry
    existing_order = next(
        (
            found_order
            for found_order, found_client in registry.broker_orders.items()
            if found_client == client_order_id
        ),
        None,
    )
    if existing_order is not None:
        raise ValueError(
            f"client_order_id {client_order_id} is already bound to {existing_order}"
        )
    bindings = dict(registry.broker_orders)
    bindings[order_id] = client_order_id
    return OrderRegistry(
        registry.account_id,
        registry.revision + 1,
        dict(registry.intents),
        bindings,
    )
