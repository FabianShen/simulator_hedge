"""Read-only assessment of an immutable Alpha target against confirmed fills."""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from hedge_engine import OrderRegistry, StrategyLedger, combined_strategy_positions
from sim_hedge.state.registry import registry_from_payload
from sim_hedge.execution.submission import request_for_intent, validate_timestamp_freshness
from sim_hedge.state.ledger import ledger_from_payload
from sim_hedge.jsonio import (
    coerce_int as _integer,
    read_json as _read,
    require_object as _object,
    write_json,
)


TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "PARTIALLY_CANCELLED", "REJECTED"}


def assess_alpha_continuation(
    plan: Mapping[str, Any],
    registry: OrderRegistry,
    ledger: StrategyLedger,
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe shortfalls; never register, submit, or cancel an order."""

    account_id = str(plan.get("account_id") or "")
    if not account_id or registry.account_id != account_id or ledger.account_id != account_id:
        raise ValueError("Alpha plan, registry, and ledger accounts do not match")
    if reconciliation.get("account_id") != account_id:
        raise ValueError("reconciliation belongs to another account")
    if reconciliation.get("registry_revision") != registry.revision or reconciliation.get("ledger_revision") != ledger.revision:
        raise ValueError("reconciliation is stale; run broker reconciliation again")
    if not reconciliation.get("position_match") or not reconciliation.get("account_healthy"):
        raise ValueError("broker position or account health is not confirmed")
    if registry.unknown_client_order_ids or reconciliation.get("unresolved_submissions"):
        raise ValueError("an order submission outcome is unknown")

    legs = plan.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("saved Alpha plan has no legs")
    per_option = _positive_int(plan.get("contracts_per_option"), "contracts_per_option")
    target: dict[str, int] = {}
    for raw in legs:
        leg = _object(raw, "Alpha leg")
        instrument = str(leg.get("instrument") or "")
        if not instrument or instrument in target:
            raise ValueError("saved Alpha plan has missing or duplicate instruments")
        quantity = _integer(leg.get("quantity"), "Alpha leg quantity")
        if quantity != -per_option:
            raise ValueError("saved Alpha plan is not equal-contract short Alpha")
        target[instrument] = quantity

    alpha_intents = [
        item for item in registry.intents.values()
        if item.strategy == "ALPHA"
        and item.client_order_id not in registry.abandoned_client_order_ids
    ]
    if not alpha_intents or {item.instrument for item in alpha_intents} != set(target):
        raise ValueError("saved Alpha plan does not cover registered Alpha instruments")
    for instrument, quantity in target.items():
        if not any(item.instrument == instrument and item.quantity == quantity for item in alpha_intents):
            raise ValueError(f"initial target intent is missing for {instrument}")

    confirmed: dict[str, int] = {}
    remaining: dict[str, int] = {}
    for instrument, quantity in sorted(target.items()):
        held = ledger.alpha_positions.get(instrument, 0)
        if held > 0 or held < quantity:
            raise ValueError(f"confirmed Alpha exceeds target for {instrument}")
        confirmed[instrument] = held
        if held > quantity:
            remaining[instrument] = held - quantity
    if set(ledger.alpha_positions) - set(target):
        raise ValueError("confirmed Alpha contains instruments outside the saved target")
    broker_positions = reconciliation.get("broker_positions")
    if broker_positions != combined_strategy_positions(ledger):
        raise ValueError("reconciliation broker positions differ from combined strategy positions")

    statuses = _object(reconciliation.get("managed_order_statuses"), "managed order statuses")
    if set(statuses) != set(registry.broker_orders):
        raise ValueError("reconciliation does not cover every registered broker order")
    working_orders = sorted(
        order_id for order_id, status in statuses.items()
        if str(status).upper() not in TERMINAL_ORDER_STATUSES
    )
    bound = set(registry.broker_orders.values())
    unbound = sorted(
        client_id for client_id, intent in registry.intents.items()
        if intent.strategy == "ALPHA" and client_id not in bound
        and client_id not in registry.abandoned_client_order_ids
    )
    if reconciliation.get("unbound_order_intents") != unbound:
        raise ValueError("reconciliation and registry unbound intents differ")
    status = (
        "WAITING_FOR_BROKER" if working_orders else
        "NEEDS_TOP_UP" if remaining else
        "NEEDS_INTENT_CLEANUP" if unbound else "TARGET_REACHED"
    )
    return {
        "status": status,
        "orders_submitted": 0,
        "account_id": account_id,
        "source_alpha_plan_id": plan.get("plan_id"),
        "registry_revision": registry.revision,
        "ledger_revision": ledger.revision,
        "contracts_per_option": per_option,
        "target_positions": dict(sorted(target.items())),
        "confirmed_positions": confirmed,
        "remaining_sell_contracts": remaining,
        "total_remaining_contracts": sum(remaining.values()),
        "working_order_ids": working_orders,
        "unsubmitted_intent_ids": unbound,
    }


def select_alpha_continuation_request(
    assessment: Mapping[str, Any],
    market: Mapping[str, Any],
    registry: OrderRegistry,
    *,
    now: datetime,
    max_age: float,
    retry_after: Mapping[str, datetime] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Select one existing intent; the simulator resolves COUNTERPARTY pricing."""

    if assessment.get("status") not in {"NEEDS_TOP_UP", "WAITING_FOR_BROKER"}:
        return None, str(assessment.get("status") or "NOT_READY")
    if assessment.get("account_id") != registry.account_id:
        raise ValueError("Alpha assessment and registry accounts do not match")
    working_order_ids = assessment.get("working_order_ids") or ()
    working_instruments = {
        registry.intents[registry.broker_orders[str(order_id)]].instrument
        for order_id in working_order_ids
    }
    validate_timestamp_freshness(
        "Alpha market snapshot", market.get("asOf"), now=now, max_age=max_age
    )
    if not market.get("requestId"):
        raise ValueError("Alpha market snapshot ID is missing")
    remaining = _object(
        assessment.get("remaining_sell_contracts"), "remaining Alpha contracts"
    )
    retry_after = retry_after or {}
    for client_id in assessment.get("unsubmitted_intent_ids", ()):
        intent = registry.intents[str(client_id)]
        if intent.strategy != "ALPHA" or intent.quantity >= 0:
            raise ValueError("continuation request is not a short Alpha intent")
        if intent.instrument in working_instruments:
            continue
        if _integer(remaining.get(intent.instrument), "remaining Alpha contracts") != -intent.quantity:
            raise ValueError(f"unsubmitted intent exceeds shortfall for {intent.instrument}")
        if now < retry_after.get(str(client_id), now):
            continue
        return request_for_intent(intent), "READY"
    return None, "WAITING_FOR_BROKER" if working_order_ids else "WAITING_FOR_RETRY"


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess Alpha shortfalls without trading")
    parser.add_argument("alpha_plan")
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("reconciliation")
    parser.add_argument("--output", default="outputs/alpha_continuation.json")
    args = parser.parse_args()
    try:
        report = assess_alpha_continuation(
            _object(_read(args.alpha_plan), "Alpha plan"),
            registry_from_payload(_object(_read(args.order_registry), "order registry")),
            ledger_from_payload(_object(_read(args.strategy_ledger), "strategy ledger")),
            _object(_read(args.reconciliation), "reconciliation"),
        )
        write_json(args.output, report)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Alpha continuation assessment failed: {exc}") from exc
    print(
        f"Alpha {report['status']}: remaining={report['total_remaining_contracts']} "
        f"working_orders={len(report['working_order_ids'])} "
        f"unsubmitted_intents={len(report['unsubmitted_intent_ids'])}; orders submitted=0"
    )
    print(f"wrote assessment: {args.output}")


def _positive_int(value: Any, name: str) -> int:
    number = _integer(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


if __name__ == "__main__":
    main()
