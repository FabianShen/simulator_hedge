"""Shared validation and sequential execution for registered order requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    abandon_unsubmitted_intents,
    bind_broker_order,
    mark_submission_unknown,
)
from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingUnknownOutcomeError,
)
from sim_hedge.jsonio import parse_iso_utc, require_object as _object


Submit = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Persist = Callable[[OrderRegistry], None]


def execute_registered_requests(
    registry: OrderRegistry,
    requests: Sequence[Mapping[str, Any]],
    *,
    submit: Submit,
    persist: Persist,
) -> tuple[OrderRegistry, dict[str, list[dict[str, str]]]]:
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    current = registry
    for idx, request in enumerate(requests):
        client_order_id = str(request["client_order_id"])
        try:
            response = submit(request)
        except SimTradingUnknownOutcomeError as exc:
            current = mark_submission_unknown(current, client_order_id)
            persist(current)
            unknown.append({"client_order_id": client_order_id, "error": str(exc)})
            break
        except SimTradingError as exc:
            rejected.append({"client_order_id": client_order_id, "error": str(exc)})
            orphaned = tuple(
                str(request["client_order_id"]) for request in requests[idx:]
            )
            current = abandon_unsubmitted_intents(current, orphaned)
            persist(current)
            break
        order_id = str(response["order_id"])
        current = bind_broker_order(
            current, client_order_id=client_order_id, order_id=order_id
        )
        persist(current)
        accepted.append({"client_order_id": client_order_id, "order_id": order_id})
    return current, {
        "accepted": accepted,
        "rejected": rejected,
        "unknown": unknown,
    }


def validate_registered_requests(
    raw_requests: Any,
    registry: OrderRegistry,
    *,
    strategy: str,
    max_total_contracts: int,
) -> tuple[Mapping[str, Any], ...]:
    if max_total_contracts <= 0:
        raise ValueError("max_total_contracts must be positive")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValueError("dry-run requests must be a non-empty list")
    requests = tuple(_object(value, "dry-run request") for value in raw_requests)
    total_contracts = sum(int(value.get("volume") or 0) for value in requests)
    if total_contracts > max_total_contracts:
        raise ValueError(
            f"dry-run total volume {total_contracts} exceeds explicit limit "
            f"{max_total_contracts}"
        )
    client_ids = [str(value.get("client_order_id") or "") for value in requests]
    if len(client_ids) != len(set(client_ids)):
        raise ValueError("dry-run contains duplicate client_order_id values")
    for request, client_id in zip(requests, client_ids):
        intent = registry.intents.get(client_id)
        if intent is None:
            raise ValueError(f"unregistered client_order_id: {client_id}")
        if intent.strategy != strategy:
            raise ValueError(f"client_order_id is not owned by {strategy}: {client_id}")
        if client_id in registry.abandoned_client_order_ids:
            raise ValueError(f"client_order_id was abandoned: {client_id}")
        if client_id in registry.broker_orders.values():
            raise ValueError(f"client_order_id already submitted: {client_id}")
        if client_id in registry.unknown_client_order_ids:
            raise ValueError(f"client_order_id has unknown submission state: {client_id}")
        if dict(request) != request_for_intent(intent):
            raise ValueError(f"dry-run request conflicts with registry: {client_id}")
    return requests


def validate_timestamp_freshness(
    name: str, value: Any, *, now: datetime, max_age: float
) -> None:
    """Validate one protocol timestamp without requiring a pricing payload."""

    if now.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    if max_age <= 0:
        raise ValueError("max_snapshot_age_seconds must be positive")
    _require_fresh(name, value, now=now, max_age=max_age)


def request_for_intent(intent: OrderIntent) -> dict[str, Any]:
    request = {
        "client_order_id": intent.client_order_id,
        "account_id": intent.account_id,
        "exchange_id": intent.exchange_id,
        "symbol": intent.instrument,
        "direction": "BUY" if intent.quantity > 0 else "SELL",
        "offset_flag": intent.offset,
        "order_type": intent.order_type,
        "volume": abs(intent.quantity),
    }
    if intent.limit_price is not None:
        request["limit_price"] = str(intent.limit_price)
    return request


def _require_fresh(name: str, value: Any, *, now: datetime, max_age: float) -> None:
    observed = parse_iso_utc(str(value or ""), "pricing timestamp")
    age = (now - observed).total_seconds()
    if age < 0 or age > max_age:
        raise ValueError(f"{name} is stale: age={age:g}s")
