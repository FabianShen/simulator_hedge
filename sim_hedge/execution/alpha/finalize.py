"""Adopt broker-confirmed Alpha holdings as the final initialization target."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderRegistry, StrategyLedger, abandon_unsubmitted_intents,
    strategy_intents_fully_filled,
    combined_strategy_positions,
)
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.state.ledger import ledger_from_payload


def finalize_alpha(
    plan: Mapping[str, Any], registry: OrderRegistry, ledger: StrategyLedger,
    reconciliation: Mapping[str, Any],
) -> tuple[dict[str, Any], OrderRegistry]:
    """Retire only unsubmitted legs; never change confirmed positions or orders."""

    account = registry.account_id
    if not plan.get("plan_id") or plan.get("account_id") != account or ledger.account_id != account:
        raise ValueError("Alpha plan, registry, and ledger accounts do not match")
    if reconciliation.get("account_id") != account:
        raise ValueError("reconciliation belongs to another account")
    if (reconciliation.get("registry_revision") != registry.revision
            or reconciliation.get("ledger_revision") != ledger.revision):
        raise ValueError("reconciliation is stale; run broker reconciliation again")
    if registry.unknown_client_order_ids or reconciliation.get("unresolved_submissions"):
        raise ValueError("unknown submission outcome; resolve it with the broker first")
    if not ledger.alpha_positions:
        raise ValueError("adoption requires confirmed Alpha and no Beta positions")
    if not reconciliation.get("position_match") or not reconciliation.get("account_healthy"):
        raise ValueError("broker positions or account health are not confirmed")
    if reconciliation.get("broker_positions") != combined_strategy_positions(ledger):
        raise ValueError("broker positions differ from confirmed Alpha")
    if reconciliation.get("active_order_ids"):
        raise ValueError("broker still has active orders")

    legs = plan.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("saved Alpha plan has no legs")
    planned = {str(leg.get("instrument")) for leg in legs if isinstance(leg, Mapping)}
    if not set(ledger.alpha_positions) <= planned:
        raise ValueError("confirmed Alpha includes instruments outside the saved plan")
    statuses = reconciliation.get("managed_order_statuses")
    if not isinstance(statuses, Mapping) or set(statuses) != set(registry.broker_orders):
        raise ValueError("reconciliation does not cover every registered broker order")
    if any(str(status).upper() != "FILLED" for status in statuses.values()):
        raise ValueError("a registered broker order is not fully filled")

    bound = set(registry.broker_orders.values())
    pending = tuple(sorted(
        client_id for client_id, intent in registry.intents.items()
        if intent.strategy == "ALPHA" and client_id not in bound
        and client_id not in registry.abandoned_client_order_ids
    ))
    if reconciliation.get("unbound_order_intents") != list(pending):
        raise ValueError("reconciliation and registry unbound intents differ")
    if any(
        intent.strategy != "ALPHA" and client_id not in bound
        and client_id not in registry.abandoned_client_order_ids
        for client_id, intent in registry.intents.items()
    ):
        raise ValueError("unsubmitted non-Alpha intents exist")
    updated = abandon_unsubmitted_intents(registry, pending)
    if not strategy_intents_fully_filled(updated, ledger, "ALPHA"):
        raise ValueError("remaining Alpha intents are not fully confirmed by broker trades")
    adoption = {
        "version": "sim-hedge/alpha-adoption/v1",
        "account_id": account,
        "source_alpha_plan_id": plan["plan_id"],
        "source_registry_revision": registry.revision,
        "registry_revision": updated.revision,
        "ledger_revision": ledger.revision,
        "alpha_positions": dict(sorted(ledger.alpha_positions.items())),
        "abandoned_client_order_ids": list(pending),
        "orders_submitted": 0,
    }
    return adoption, updated


def validate_alpha_adoption(
    adoption: Mapping[str, Any], plan: Mapping[str, Any],
    registry: OrderRegistry, ledger: StrategyLedger,
) -> None:
    if adoption.get("version") != "sim-hedge/alpha-adoption/v1":
        raise ValueError("unknown Alpha adoption version")
    if (adoption.get("account_id") != ledger.account_id
            or registry.account_id != ledger.account_id
            or adoption.get("source_alpha_plan_id") != plan.get("plan_id")):
        raise ValueError("Alpha adoption account or plan does not match")
    if (registry.revision < adoption.get("registry_revision", -1)
            or ledger.revision < adoption.get("ledger_revision", -1)):
        raise ValueError("Alpha adoption references newer registry or ledger state")
    if adoption.get("alpha_positions") != dict(ledger.alpha_positions):
        raise ValueError("confirmed Alpha differs from adopted Alpha positions")
    if not set(adoption.get("abandoned_client_order_ids", ())) <= set(registry.abandoned_client_order_ids):
        raise ValueError("Alpha adoption is not applied to the registry")
    if registry.unknown_client_order_ids:
        raise ValueError("order registry contains unknown submissions")


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze confirmed Alpha; never submit or cancel orders")
    parser.add_argument("alpha_plan")
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("reconciliation")
    parser.add_argument("--output", required=True, help="Alpha adoption record (auto_trader output dir)")
    args = parser.parse_args()
    try:
        registry_path = Path(args.order_registry)
        destination = Path(args.output)
        if destination.exists():
            raise ValueError("Alpha adoption already exists; inspect it before proceeding")
        plan = _read(args.alpha_plan)
        registry = registry_from_payload(_read(registry_path))
        ledger = ledger_from_payload(_read(args.strategy_ledger))
        adoption, updated = finalize_alpha(plan, registry, ledger, _read(args.reconciliation))
        # The marker is written first: a crash cannot let auto_trader resume top-ups.
        _write(destination, adoption)
        _write(registry_path, registry_to_payload(updated))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Alpha adoption blocked: {exc}") from exc
    print(f"adopted {len(adoption['alpha_positions'])} confirmed Alpha positions; "
          f"retired {len(adoption['abandoned_client_order_ids'])} unsubmitted intents; "
          "orders submitted=0")
    print(f"wrote Alpha adoption: {args.output}")


def _read(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
