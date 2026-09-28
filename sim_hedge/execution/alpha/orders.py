"""Build and register dry-run Alpha order intents without submitting them."""

from __future__ import annotations

import argparse
from decimal import DecimalException
from hashlib import sha256
import json
from typing import Any, Mapping

from hedge_engine import OrderIntent, OrderRegistry, empty_order_registry, register_order_intent
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.jsonio import (
    coerce_int as _integer,
    parse_iso_utc,
    read_json as _read,
    require_object as _object,
    write_json as _write,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dry-run Alpha order intents")
    parser.add_argument("alpha_plan")
    parser.add_argument("--exchange-id", required=True)
    parser.add_argument("--registry", help="existing order registry")
    parser.add_argument("--output", default="outputs/alpha_order_dry_run.json")
    parser.add_argument("--registry-output", default="outputs/order_registry.json")
    args = parser.parse_args()
    try:
        alpha = _object(_read(args.alpha_plan), "Alpha plan")
        registry = (
            registry_from_payload(_object(_read(args.registry), "order registry"))
            if args.registry
            else None
        )
        dry_run, updated = build_alpha_order_dry_run(
            alpha,
            exchange_id=args.exchange_id,
            registry=registry,
        )
        _write(args.output, dry_run)
        _write(args.registry_output, registry_to_payload(updated))
    except (ValueError, KeyError, DecimalException, json.JSONDecodeError) as exc:
        raise SystemExit(f"Alpha order dry-run failed: {exc}") from exc
    print(
        f"registered {len(dry_run['requests'])} dry-run Alpha intents; "
        "orders submitted=0"
    )
    print(f"wrote dry-run requests: {args.output}")
    print(f"wrote order registry: {args.registry_output}")


def build_alpha_order_dry_run(
    alpha: Mapping[str, Any],
    *,
    exchange_id: str,
    registry: OrderRegistry | None = None,
) -> tuple[dict[str, Any], OrderRegistry]:
    if not exchange_id:
        raise ValueError("exchange_id must not be empty")
    request_id = str(alpha.get("source_alpha_market_id") or "")
    if not request_id:
        raise ValueError("Alpha plan market snapshot ID must not be empty")
    if alpha.get("orders_generated") is not False:
        raise ValueError("expected an offline Alpha plan")
    account_id = str(alpha.get("account_id") or "")
    if not account_id:
        raise ValueError("Alpha plan account_id must not be empty")
    current = registry or empty_order_registry(account_id)
    if current.account_id != account_id:
        raise ValueError("Alpha plan and order registry account IDs do not match")
    legs = alpha.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("Alpha legs must be a non-empty list")
    created_at = parse_iso_utc(str(alpha.get("as_of") or ""), "pricing asOf")
    requests = []
    for raw_leg in legs:
        leg = _object(raw_leg, "Alpha leg")
        instrument = str(leg.get("instrument") or "")
        quantity = _integer(leg.get("quantity"), f"Alpha quantity for {instrument}")
        if quantity >= 0:
            raise ValueError("initial Alpha order quantities must be negative")
        client_order_id = _client_order_id(request_id, instrument)
        intent = OrderIntent(
            client_order_id=client_order_id,
            account_id=account_id,
            strategy="ALPHA",
            exchange_id=exchange_id,
            instrument=instrument,
            quantity=quantity,
            offset="OPEN",
            order_type="COUNTERPARTY",
            limit_price=None,
            created_at=created_at,
        )
        current = register_order_intent(current, intent)
        requests.append(
            {
                "client_order_id": client_order_id,
                "account_id": account_id,
                "exchange_id": exchange_id,
                "symbol": instrument,
                "direction": "SELL",
                "offset_flag": "OPEN",
                "order_type": "COUNTERPARTY",
                "volume": abs(quantity),
            }
        )
    return (
        {
            "source_alpha_market_id": request_id,
            "source_alpha_plan_id": alpha.get("plan_id"),
            "source_market_as_of": alpha.get("as_of"),
            "price_source": "SIMULATOR_COUNTERPARTY_RESOLUTION",
            "order_type": "COUNTERPARTY",
            "submission_allowed": False,
            "orders_submitted": 0,
            "requests": requests,
        },
        current,
    )


def _client_order_id(request_id: str, instrument: str) -> str:
    digest = sha256(f"{request_id}:{instrument}".encode("utf-8")).hexdigest()[:16]
    return f"alpha-{instrument}-{digest}"


if __name__ == "__main__":
    main()
