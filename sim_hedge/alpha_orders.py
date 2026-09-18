"""Build and register dry-run Alpha order intents without submitting them."""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import OrderIntent, OrderRegistry, empty_order_registry, register_order_intent
from sim_hedge.order_registry import registry_from_payload, registry_to_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dry-run Alpha order intents")
    parser.add_argument("pricing_request")
    parser.add_argument("alpha_plan")
    parser.add_argument("--exchange-id", required=True)
    parser.add_argument("--registry", help="existing order registry")
    parser.add_argument("--output", default="outputs/alpha_order_dry_run.json")
    parser.add_argument("--registry-output", default="outputs/order_registry.json")
    args = parser.parse_args()
    try:
        pricing = _object(_read(args.pricing_request), "pricing request")
        alpha = _object(_read(args.alpha_plan), "Alpha plan")
        registry = (
            registry_from_payload(_object(_read(args.registry), "order registry"))
            if args.registry
            else None
        )
        dry_run, updated = build_alpha_order_dry_run(
            pricing,
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
    pricing: Mapping[str, Any],
    alpha: Mapping[str, Any],
    *,
    exchange_id: str,
    registry: OrderRegistry | None = None,
) -> tuple[dict[str, Any], OrderRegistry]:
    if not exchange_id:
        raise ValueError("exchange_id must not be empty")
    request_id = str(pricing.get("requestId") or "")
    if not request_id or alpha.get("source_pricing_request_id") != request_id:
        raise ValueError("Alpha plan and pricing request IDs do not match")
    if alpha.get("orders_generated") is not False:
        raise ValueError("expected an offline Alpha plan")
    account_id = str(alpha.get("account_id") or "")
    if not account_id:
        raise ValueError("Alpha plan account_id must not be empty")
    current = registry or empty_order_registry(account_id)
    if current.account_id != account_id:
        raise ValueError("Alpha plan and order registry account IDs do not match")
    options = pricing.get("options")
    legs = alpha.get("legs")
    if not isinstance(options, list) or not isinstance(legs, list) or not legs:
        raise ValueError("pricing options and non-empty Alpha legs must be lists")
    metadata = {
        str(_object(option, "pricing option")["instrument"]): option
        for option in options
    }
    created_at = _datetime(str(pricing.get("asOf") or ""))
    requests = []
    for raw_leg in legs:
        leg = _object(raw_leg, "Alpha leg")
        instrument = str(leg.get("instrument") or "")
        quantity = _integer(leg.get("quantity"), f"Alpha quantity for {instrument}")
        if quantity >= 0:
            raise ValueError("initial Alpha order quantities must be negative")
        option = _object(metadata.get(instrument), f"pricing option {instrument}")
        tick = Decimal(str(option["priceTick"]))
        market_price = Decimal(str(option["marketPrice"]))
        if tick <= 0 or market_price <= 0:
            raise ValueError(f"invalid market price or tick for {instrument}")
        limit_price = (market_price / tick).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ) * tick
        client_order_id = _client_order_id(request_id, instrument)
        intent = OrderIntent(
            client_order_id=client_order_id,
            account_id=account_id,
            strategy="ALPHA",
            instrument=instrument,
            quantity=quantity,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=limit_price,
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
                "order_type": "LIMIT",
                "limit_price": str(limit_price),
                "volume": abs(quantity),
            }
        )
    return (
        {
            "source_pricing_request_id": request_id,
            "source_alpha_plan_id": alpha.get("plan_id"),
            "price_source": "RECORDED_MID_ROUNDED_TO_TICK",
            "submission_allowed": False,
            "orders_submitted": 0,
            "requests": requests,
        },
        current,
    )


def _client_order_id(request_id: str, instrument: str) -> str:
    digest = sha256(f"{request_id}:{instrument}".encode("utf-8")).hexdigest()[:16]
    return f"alpha-{instrument}-{digest}"


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
        raise ValueError("pricing asOf must be timezone-aware")
    return result


if __name__ == "__main__":
    main()
