"""Explicitly adopt a manually filled replacement as an Alpha-owned order."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    StrategyLedger,
    apply_confirmed_fills,
    bind_broker_order,
    register_order_intent,
    strategy_intents_fully_filled,
    supersede_order_intent,
)
from sim_hedge.adapters.sim_trading import (
    BrokerOrder,
    SimTradingError,
    SimTradingPortfolioSource,
)
from sim_hedge.config import load_env_file
from sim_hedge.execution.alpha.reconcile import load_all_fills
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.state.ledger import ledger_from_payload, ledger_to_payload


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Adopt one manually filled replacement Alpha order"
    )
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("--supersede", required=True, metavar="CLIENT_ORDER_ID")
    parser.add_argument("--replacement-order", required=True, metavar="ORDER_ID")
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--registry-output")
    parser.add_argument("--ledger-output")
    parser.add_argument("--report-output", default="outputs/alpha_adoption.json")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_output = args.registry_output or args.order_registry
    ledger_output = args.ledger_output or args.strategy_ledger
    try:
        registry = registry_from_payload(
            _object(_read(args.order_registry), "order registry")
        )
        ledger = ledger_from_payload(
            _object(_read(args.strategy_ledger), "strategy ledger")
        )
        source = SimTradingPortfolioSource(
            args.base_url,
            access_token=os.getenv("SIM_ACCESS_TOKEN"),
        )
        if not os.getenv("SIM_ACCESS_TOKEN"):
            username = os.getenv("SIM_USERNAME")
            password = os.getenv("SIM_PASSWORD")
            if not username or not password:
                parser.error(
                    "set SIM_ACCESS_TOKEN, or set both SIM_USERNAME and SIM_PASSWORD"
                )
            source.login(username, password)
        updated_registry, updated_ledger, report = adopt_alpha_replacement(
            source=source,
            registry=registry,
            ledger=ledger,
            original_client_order_id=args.supersede,
            replacement_order_id=args.replacement_order,
        )
        _write(registry_output, registry_to_payload(updated_registry))
        _write(ledger_output, ledger_to_payload(updated_ledger))
        _write(args.report_output, report)
    except (ValueError, KeyError, json.JSONDecodeError, SimTradingError) as exc:
        raise SystemExit(f"Alpha adoption failed: {exc}") from exc
    print(
        f"adopted replacement order {args.replacement_order} as ALPHA; "
        f"ledger revision={updated_ledger.revision}"
    )
    print(f"superseded intent: {args.supersede}")
    print(f"wrote order registry: {registry_output}")
    print(f"wrote strategy ledger: {ledger_output}")
    print(f"wrote adoption audit: {args.report_output}")


def adopt_alpha_replacement(
    *,
    source: SimTradingPortfolioSource,
    registry: OrderRegistry,
    ledger: StrategyLedger,
    original_client_order_id: str,
    replacement_order_id: str,
) -> tuple[OrderRegistry, StrategyLedger, dict[str, Any]]:
    if registry.account_id != ledger.account_id:
        raise ValueError("order registry and strategy ledger accounts do not match")
    original_intent = registry.intents.get(original_client_order_id)
    if original_intent is None or original_intent.strategy != "ALPHA":
        raise ValueError("--supersede must identify a registered Alpha intent")
    original_order_id = _order_id_for_client(registry, original_client_order_id)
    original_order = source.load_order(original_order_id, registry.account_id)
    replacement_order = source.load_order(replacement_order_id, registry.account_id)
    _validate_cancelled_original(original_order, original_intent)
    _validate_filled_replacement(replacement_order, original_intent)

    replacement_intent = OrderIntent(
        client_order_id=replacement_order.client_order_id,
        account_id=replacement_order.account_id,
        strategy="ALPHA",
        exchange_id=replacement_order.exchange_id,
        instrument=replacement_order.instrument,
        quantity=_signed_quantity(replacement_order),
        offset=replacement_order.offset,
        order_type=replacement_order.order_type,
        limit_price=replacement_order.limit_price,
        created_at=replacement_order.created_at,
    )
    updated_registry = register_order_intent(registry, replacement_intent)
    updated_registry = bind_broker_order(
        updated_registry,
        client_order_id=replacement_intent.client_order_id,
        order_id=replacement_order.order_id,
    )
    updated_registry = supersede_order_intent(
        updated_registry,
        original_client_order_id=original_client_order_id,
        replacement_client_order_id=replacement_intent.client_order_id,
    )
    fills = load_all_fills(source, updated_registry)
    replacement_fills = tuple(
        fill for fill in fills if fill.order_id == replacement_order.order_id
    )
    if sum((fill.quantity for fill in replacement_fills), start=0) != original_intent.quantity:
        raise ValueError("replacement broker fills do not equal the original quantity")
    updated_ledger = apply_confirmed_fills(ledger, fills)
    if not strategy_intents_fully_filled(updated_registry, updated_ledger, "ALPHA"):
        raise ValueError("Alpha intents are not fully filled after adoption")
    return updated_registry, updated_ledger, {
        "status": "ADOPTED",
        "account_id": registry.account_id,
        "strategy": "ALPHA",
        "superseded_client_order_id": original_client_order_id,
        "superseded_order_id": original_order_id,
        "replacement_client_order_id": replacement_intent.client_order_id,
        "replacement_order_id": replacement_order.order_id,
        "replacement_trade_ids": sorted(fill.trade_id for fill in replacement_fills),
        "instrument": replacement_intent.instrument,
        "quantity": replacement_intent.quantity,
        "registry_revision": updated_registry.revision,
        "ledger_revision": updated_ledger.revision,
    }


def _validate_cancelled_original(order: BrokerOrder, intent: OrderIntent) -> None:
    if order.client_order_id != intent.client_order_id:
        raise ValueError("original broker order does not match the saved intent")
    actual = (
        order.account_id,
        order.exchange_id,
        order.instrument,
        _signed_quantity(order),
        order.offset,
        order.order_type,
        order.limit_price,
    )
    expected = (
        intent.account_id,
        intent.exchange_id,
        intent.instrument,
        intent.quantity,
        intent.offset,
        intent.order_type,
        intent.limit_price,
    )
    if actual != expected:
        raise ValueError("original broker order conflicts with the saved intent")
    if order.status != "CANCELLED" or order.traded_volume != 0:
        raise ValueError("original broker order must be cancelled with zero fills")
    if order.cancelled_volume != order.total_volume:
        raise ValueError("original broker order is not fully cancelled")


def _validate_filled_replacement(order: BrokerOrder, intent: OrderIntent) -> None:
    if order.status != "FILLED":
        raise ValueError("replacement broker order must be FILLED")
    if order.traded_volume != order.total_volume or order.remaining_volume != 0:
        raise ValueError("replacement broker order is not fully filled")
    actual = (
        order.account_id,
        order.exchange_id,
        order.instrument,
        _signed_quantity(order),
        order.offset,
        order.order_type,
    )
    expected = (
        intent.account_id,
        intent.exchange_id,
        intent.instrument,
        intent.quantity,
        intent.offset,
        intent.order_type,
    )
    if actual != expected:
        raise ValueError("replacement broker order does not match the Alpha intent")


def _signed_quantity(order: BrokerOrder) -> int:
    if order.direction == "BUY":
        return order.total_volume
    if order.direction == "SELL":
        return -order.total_volume
    raise ValueError(f"unknown broker order direction: {order.direction!r}")


def _order_id_for_client(registry: OrderRegistry, client_order_id: str) -> str:
    matches = [
        order_id
        for order_id, saved_client_id in registry.broker_orders.items()
        if saved_client_id == client_order_id
    ]
    if len(matches) != 1:
        raise ValueError("superseded intent must have exactly one broker order")
    return matches[0]


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


if __name__ == "__main__":
    main()
