"""Persist order ownership before submission and bind accepted broker IDs."""

from __future__ import annotations

import argparse
from decimal import Decimal, DecimalException
import json
from typing import Any, Mapping

from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    bind_broker_order,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.jsonio import (
    coerce_int as _integer,
    parse_iso_utc,
    read_json as _read,
    require_object as _object,
    write_json as _write,
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
        created_at=parse_iso_utc(
            str(payload.get("created_at") or ""), "order created_at"
        ),
        proposal_id=(
            None
            if payload.get("proposal_id") in (None, "")
            else str(payload["proposal_id"])
        ),
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
                "proposal_id": intent.proposal_id,
            }
            for client_id, intent in sorted(registry.intents.items())
        },
        "broker_orders": dict(sorted(registry.broker_orders.items())),
        "unknown_client_order_ids": list(registry.unknown_client_order_ids),
        "superseded_client_order_ids": dict(
            sorted(registry.superseded_client_order_ids.items())
        ),
        "abandoned_client_order_ids": list(registry.abandoned_client_order_ids),
        "retired_cancelled_client_order_ids": list(
            registry.retired_cancelled_client_order_ids
        ),
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
    raw_superseded = _object(
        payload.get("superseded_client_order_ids", {}), "superseded client order IDs"
    )
    raw_abandoned = payload.get("abandoned_client_order_ids", [])
    if not isinstance(raw_abandoned, list):
        raise ValueError("abandoned_client_order_ids must be a list")
    raw_retired = payload.get("retired_cancelled_client_order_ids", [])
    if not isinstance(raw_retired, list):
        raise ValueError("retired_cancelled_client_order_ids must be a list")
    return OrderRegistry(
        account_id=account_id,
        revision=_integer(payload.get("revision"), "registry revision"),
        intents=intents,
        broker_orders={str(key): str(value) for key, value in raw_bindings.items()},
        unknown_client_order_ids=tuple(str(value) for value in raw_unknown),
        superseded_client_order_ids={
            str(key): str(value) for key, value in raw_superseded.items()
        },
        abandoned_client_order_ids=tuple(str(value) for value in raw_abandoned),
        retired_cancelled_client_order_ids=tuple(str(value) for value in raw_retired),
    )


if __name__ == "__main__":
    main()
