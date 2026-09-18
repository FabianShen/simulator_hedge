"""Persist order ownership before submission and bind accepted broker IDs."""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, DecimalException
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    bind_broker_order,
    empty_order_registry,
    register_order_intent,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain the order ownership registry")
    parser.add_argument("operation", choices=("register", "bind"))
    parser.add_argument("input", help="order intent or broker binding JSON")
    parser.add_argument("--registry", help="existing registry JSON")
    parser.add_argument("--output", default="outputs/order_registry.json")
    args = parser.parse_args()
    try:
        value = _object(_read(args.input), "input")
        if args.operation == "register":
            intent = intent_from_payload(value)
            registry = (
                registry_from_payload(_object(_read(args.registry), "registry"))
                if args.registry
                else empty_order_registry(intent.account_id)
            )
            updated = register_order_intent(registry, intent)
        else:
            if not args.registry:
                raise ValueError("bind requires --registry")
            registry = registry_from_payload(
                _object(_read(args.registry), "registry")
            )
            updated = bind_broker_order(
                registry,
                client_order_id=str(value.get("client_order_id") or ""),
                order_id=str(value.get("order_id") or ""),
            )
        _write(args.output, registry_to_payload(updated))
    except (ValueError, KeyError, DecimalException, json.JSONDecodeError) as exc:
        raise SystemExit(f"order registry failed: {exc}") from exc
    print(
        f"order registry revision={updated.revision} "
        f"intents={len(updated.intents)} bound={len(updated.broker_orders)}"
    )
    print(f"wrote order registry: {args.output}")


def intent_from_payload(payload: Mapping[str, Any]) -> OrderIntent:
    raw_price = payload.get("limit_price")
    return OrderIntent(
        client_order_id=str(payload.get("client_order_id") or ""),
        account_id=str(payload.get("account_id") or ""),
        strategy=str(payload.get("strategy") or "").upper(),
        exchange_id=str(payload.get("exchange_id") or ""),
        instrument=str(payload.get("instrument") or ""),
        quantity=_integer(payload.get("quantity"), "order quantity"),
        offset=str(payload.get("offset") or "").upper(),
        order_type=str(payload.get("order_type") or "").upper(),
        limit_price=(
            None if raw_price in (None, "") else Decimal(str(raw_price))
        ),
        created_at=_datetime(str(payload.get("created_at") or "")),
    )


def registry_to_payload(registry: OrderRegistry) -> dict[str, Any]:
    return {
        "status": "ACTIVE",
        "account_id": registry.account_id,
        "revision": registry.revision,
        "intents": {
            client_id: {
                "account_id": intent.account_id,
                "strategy": intent.strategy,
                "exchange_id": intent.exchange_id,
                "instrument": intent.instrument,
                "quantity": intent.quantity,
                "offset": intent.offset,
                "order_type": intent.order_type,
                "limit_price": (
                    None if intent.limit_price is None else str(intent.limit_price)
                ),
                "created_at": intent.created_at.isoformat(),
            }
            for client_id, intent in sorted(registry.intents.items())
        },
        "broker_orders": dict(sorted(registry.broker_orders.items())),
        "unknown_client_order_ids": list(registry.unknown_client_order_ids),
    }


def registry_from_payload(payload: Mapping[str, Any]) -> OrderRegistry:
    if payload.get("status") != "ACTIVE":
        raise ValueError("order registry status must be ACTIVE")
    account_id = str(payload.get("account_id") or "")
    raw_intents = _object(payload.get("intents"), "order intents")
    intents = {
        str(client_id): intent_from_payload(
            {
                **_object(raw, "order intent"),
                "client_order_id": client_id,
                "account_id": account_id,
            }
        )
        for client_id, raw in raw_intents.items()
    }
    raw_bindings = _object(payload.get("broker_orders"), "broker orders")
    raw_unknown = payload.get("unknown_client_order_ids", [])
    if not isinstance(raw_unknown, list):
        raise ValueError("unknown_client_order_ids must be a list")
    return OrderRegistry(
        account_id=account_id,
        revision=_integer(payload.get("revision"), "registry revision"),
        intents=intents,
        broker_orders={str(key): str(value) for key, value in raw_bindings.items()},
        unknown_client_order_ids=tuple(str(value) for value in raw_unknown),
    )


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} is not an object")
    return value


def _integer(value: Any, name: str) -> int:
    number = Decimal(str(value))
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("order created_at must be timezone-aware")
    return result


if __name__ == "__main__":
    main()
