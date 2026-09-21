"""Build and register dry-run Beta hedge orders without submitting them."""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    StrategyLedger,
    abandon_unsubmitted_intents,
    register_order_intent,
    strategy_intents_fully_filled,
)
from hedge_engine import validate_hedge_proposal
from sim_hedge.order_registry import registry_from_payload, registry_to_payload
from sim_hedge.order_submission import request_for_intent
from sim_hedge.strategy_ledger import ledger_from_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dry-run Beta hedge orders")
    parser.add_argument("accepted_hedge_proposal")
    parser.add_argument("strategy_ledger")
    parser.add_argument("order_registry")
    parser.add_argument("--exchange-id", required=True)
    parser.add_argument("--max-total-contracts", required=True, type=int)
    parser.add_argument(
        "--replace-unsubmitted",
        action="store_true",
        help="abandon earlier unsubmitted Beta intents before saving this proposal",
    )
    parser.add_argument("--output", default="outputs/beta_order_dry_run.json")
    parser.add_argument("--registry-output")
    args = parser.parse_args()
    registry_output = args.registry_output or args.order_registry
    try:
        hedge = _object(_read(args.accepted_hedge_proposal), "hedge proposal")
        ledger = ledger_from_payload(
            _object(_read(args.strategy_ledger), "strategy ledger")
        )
        registry = registry_from_payload(
            _object(_read(args.order_registry), "order registry")
        )
        dry_run, updated = build_beta_order_dry_run(
            hedge,
            ledger,
            registry,
            exchange_id=args.exchange_id,
            max_total_contracts=args.max_total_contracts,
            replace_unsubmitted=args.replace_unsubmitted,
        )
        _write(args.output, dry_run)
        _write(registry_output, registry_to_payload(updated))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Beta order dry-run failed: {exc}") from exc
    if dry_run["requests"]:
        print(
            f"registered {len(dry_run['requests'])} dry-run Beta intents; "
            f"total contracts={dry_run['total_contracts']} orders submitted=0"
        )
    else:
        print("Beta dry-run: NO_ACTION; intents registered=0 orders submitted=0")
    print(f"wrote dry-run requests: {args.output}")
    print(f"wrote order registry: {registry_output}")


def build_beta_order_dry_run(
    hedge: Mapping[str, Any],
    ledger: StrategyLedger,
    registry: OrderRegistry,
    *,
    exchange_id: str,
    max_total_contracts: int,
    replace_unsubmitted: bool = False,
) -> tuple[dict[str, Any], OrderRegistry]:
    if not exchange_id:
        raise ValueError("exchange_id must not be empty")
    if max_total_contracts <= 0:
        raise ValueError("max_total_contracts must be positive")
    request_id = str(hedge.get("source_pricing_request_id") or "")
    if registry.account_id != ledger.account_id:
        raise ValueError("strategy ledger and order registry accounts do not match")
    if not request_id:
        raise ValueError("pricing request ID must not be empty")
    if hedge.get("execution_batch_version") != "sim-hedge/execution-batch/v1":
        raise ValueError("Beta orders require an accepted hedge proposal")
    incremental = validate_hedge_proposal(
        hedge,
        pricing_request_id=request_id,
        account_id=ledger.account_id,
        base_ledger_revision=ledger.revision,
        confirmed_beta_positions=ledger.beta_positions,
    )
    if registry.unknown_client_order_ids:
        raise ValueError("order registry contains unknown submission outcomes")
    if not strategy_intents_fully_filled(registry, ledger, "ALPHA"):
        raise ValueError("Alpha intents are not fully confirmed by broker trades")

    alpha_instruments = set(ledger.alpha_positions)
    created_at = _datetime(str(hedge.get("created_at") or ""))
    legs: list[tuple[str, int, str]] = []
    for instrument, raw_quantity in incremental.items():
        code = str(instrument)
        quantity = _integer(raw_quantity, f"Beta trade {code}")
        if code in alpha_instruments:
            raise ValueError(f"Beta trade {code} is owned by Alpha")
        legs.extend(_split_trade(ledger.beta_positions.get(code, 0), quantity, code))

    total_contracts = sum(abs(quantity) for _, quantity, _ in legs)
    if total_contracts > max_total_contracts:
        raise ValueError(
            f"Beta total volume {total_contracts} exceeds explicit limit "
            f"{max_total_contracts}"
        )

    current = registry
    requests = []
    generated_client_ids: set[str] = set()
    for sequence, (instrument, quantity, offset) in enumerate(legs, start=1):
        client_order_id = _client_order_id(
            str(hedge["proposal_id"]),
            instrument,
            offset,
            sequence,
        )
        intent = OrderIntent(
            client_order_id=client_order_id,
            account_id=ledger.account_id,
            strategy="BETA",
            exchange_id=exchange_id,
            instrument=instrument,
            quantity=quantity,
            offset=offset,
            order_type="COUNTERPARTY",
            limit_price=None,
            created_at=created_at,
            proposal_id=str(hedge["proposal_id"]),
        )
        current = register_order_intent(current, intent)
        generated_client_ids.add(client_order_id)
        requests.append(request_for_intent(intent))

    unbound_before = (
        set(registry.intents)
        - set(registry.broker_orders.values())
        - set(registry.abandoned_client_order_ids)
        - set(registry.superseded_client_order_ids)
    )
    unrelated_unbound = unbound_before - generated_client_ids
    if unrelated_unbound:
        if not replace_unsubmitted:
            raise ValueError(
                "order registry has earlier unbound intents: "
                + ", ".join(sorted(unrelated_unbound))
            )
        if any(
            current.intents[client_id].strategy != "BETA"
            for client_id in unrelated_unbound
        ):
            raise ValueError("only earlier Beta intents may be replaced")
        current = abandon_unsubmitted_intents(
            current, tuple(sorted(unrelated_unbound))
        )
    projected = dict(ledger.beta_positions)
    for instrument, quantity in incremental.items():
        updated_quantity = projected.get(instrument, 0) + quantity
        if updated_quantity:
            projected[instrument] = updated_quantity
        else:
            projected.pop(instrument, None)
    return (
        {
            "source_hedge_proposal_id": hedge["proposal_id"],
            "source_pricing_request_id": request_id,
            "base_strategy_ledger_revision": ledger.revision,
            "strategy": "BETA",
            "source_market_as_of": hedge.get("source_market_as_of"),
            "price_source": "SIMULATOR_COUNTERPARTY_RESOLUTION",
            "order_type": "COUNTERPARTY",
            "submission_allowed": False,
            "orders_submitted": 0,
            "max_total_contracts": max_total_contracts,
            "total_contracts": total_contracts,
            "abandoned_previous_intents": sorted(unrelated_unbound),
            "current_beta_positions": dict(ledger.beta_positions),
            "incremental_trades": incremental,
            "projected_beta_positions": projected,
            "requests": requests,
        },
        current,
    )


def _split_trade(current: int, trade: int, instrument: str) -> list[tuple[str, int, str]]:
    if trade == 0:
        return []
    if current == 0 or (current > 0) == (trade > 0):
        return [(instrument, trade, "OPEN")]
    closing = min(abs(current), abs(trade))
    close_quantity = closing if trade > 0 else -closing
    result = [(instrument, close_quantity, "CLOSE")]
    remainder = trade - close_quantity
    if remainder:
        result.append((instrument, remainder, "OPEN"))
    return result


def _client_order_id(
    proposal_id: str,
    instrument: str,
    offset: str,
    sequence: int,
) -> str:
    identity = f"{proposal_id}:{instrument}:{offset}:{sequence}:COUNTERPARTY"
    digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"beta-{instrument}-{digest}"


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
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number != value:
        raise ValueError(f"{name} must be an integer")
    return number


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("hedge proposal created_at must be timezone-aware")
    return result


if __name__ == "__main__":
    main()
