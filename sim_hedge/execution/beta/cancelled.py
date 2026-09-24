"""Retire one broker-cancelled, zero-fill Beta intent without trading."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderRegistry, StrategyLedger, all_active_intents_fully_filled,
    combined_strategy_positions, retire_verified_cancelled_beta,
)
from sim_hedge.adapters.sim_trading import SimTradingError, SimTradingPortfolioSource
from sim_hedge.execution.alpha.reconcile import load_all_fills
from sim_hedge.config import load_env_file
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.state.ledger import ledger_from_payload


def verify_and_retire_cancelled_beta(
    source: SimTradingPortfolioSource,
    registry: OrderRegistry,
    ledger: StrategyLedger,
    reconciliation: Mapping[str, Any],
    order_id: str,
) -> tuple[OrderRegistry, dict[str, Any]]:
    """Confirm terminal zero-fill cancellation against fresh broker facts."""

    client_id = registry.broker_orders.get(order_id)
    intent = registry.intents.get(client_id or "")
    if intent is None or intent.strategy != "BETA":
        raise ValueError("order is not a registered Beta order")
    if client_id in registry.retired_cancelled_client_order_ids:
        raise ValueError("Beta order was already retired")
    if registry.account_id != ledger.account_id:
        raise ValueError("registry and ledger accounts do not match")
    if registry.unknown_client_order_ids:
        raise ValueError("another submission outcome is unknown")
    if (reconciliation.get("account_id") != registry.account_id
            or reconciliation.get("registry_revision") != registry.revision
            or reconciliation.get("ledger_revision") != ledger.revision):
        raise ValueError("reconciliation is stale or belongs to another account")
    if (not reconciliation.get("position_match")
            or not reconciliation.get("account_healthy")
            or reconciliation.get("active_order_ids")
            or reconciliation.get("unresolved_submissions")
            or reconciliation.get("unbound_order_intents")):
        raise ValueError("reconciliation has another unresolved broker condition")
    statuses = reconciliation.get("managed_order_statuses")
    if not isinstance(statuses, Mapping) or statuses.get(order_id) != "CANCELLED":
        raise ValueError("reconciliation does not confirm a cancelled order")
    if any(fill.order_id == order_id for fill in ledger.applied_trades.values()):
        raise ValueError("cancelled Beta order has confirmed fills")

    first_portfolio = source.load(registry.account_id)
    _check_portfolio(first_portfolio, registry, ledger)
    first_order = source.load_order(order_id, registry.account_id)
    _check_order(first_order, order_id, client_id, intent)
    first_fills = load_all_fills(source, registry)
    _check_fills(first_fills, ledger, order_id)
    second_order = source.load_order(order_id, registry.account_id)
    _check_order(second_order, order_id, client_id, intent)
    second_fills = load_all_fills(source, registry)
    _check_fills(second_fills, ledger, order_id)
    second_portfolio = source.load(registry.account_id)
    _check_portfolio(second_portfolio, registry, ledger)
    if first_portfolio.signed_positions != second_portfolio.signed_positions:
        raise ValueError("broker positions changed during verification")
    if first_order != second_order or first_fills != second_fills:
        raise ValueError("broker order or trade history changed during verification")

    updated = retire_verified_cancelled_beta(registry, client_id)
    if not all_active_intents_fully_filled(updated, ledger):
        raise ValueError("another active intent is not fully filled")
    audit = {
        "version": "sim-hedge/cancelled-beta-resolution/v1",
        "account_id": registry.account_id,
        "order_id": order_id,
        "client_order_id": client_id,
        "instrument": intent.instrument,
        "status": "CANCELLED",
        "confirmed_filled_quantity": 0,
        "source_registry_revision": registry.revision,
        "registry_revision": updated.revision,
        "ledger_revision": ledger.revision,
        "decision": "RETIRED_VERIFIED_CANCELLED_ZERO_FILL",
        "orders_submitted": 0,
    }
    return updated, audit


def _check_portfolio(portfolio, registry, ledger) -> None:
    account = portfolio.account
    if account.account_id != registry.account_id:
        raise ValueError("broker account does not match registry")
    if account.status != "NORMAL" or account.risk_state not in (None, "NORMAL"):
        raise ValueError("broker account is not healthy")
    if portfolio.active_orders:
        raise ValueError("broker still has active orders")
    if portfolio.signed_positions != combined_strategy_positions(ledger):
        raise ValueError("broker positions differ from the confirmed ledger")


def _check_order(order, order_id, client_id, intent) -> None:
    expected = (
        order_id, client_id, intent.account_id, intent.exchange_id,
        intent.instrument, "BUY" if intent.quantity > 0 else "SELL",
        intent.offset, intent.order_type, abs(intent.quantity),
    )
    actual = (
        order.order_id, order.client_order_id, order.account_id,
        order.exchange_id, order.instrument, order.direction, order.offset,
        order.order_type, order.total_volume,
    )
    if actual != expected:
        raise ValueError("broker order does not match the registered Beta intent")
    if (order.status != "CANCELLED" or order.traded_volume != 0
            or order.remaining_volume != 0
            or order.cancelled_volume != order.total_volume):
        raise ValueError("broker order is not terminal CANCELLED with zero fills")


def _check_fills(fills, ledger, order_id) -> None:
    if any(fill.order_id == order_id for fill in fills):
        raise ValueError("broker reports a fill for the cancelled Beta order")
    by_id = {fill.trade_id: fill for fill in fills}
    if len(by_id) != len(fills) or by_id != dict(ledger.applied_trades):
        raise ValueError("broker trade history differs from the confirmed ledger")


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Retire one confirmed zero-fill cancelled Beta order; never trade"
    )
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("reconciliation")
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--report-output")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_path = Path(args.order_registry)
    ledger_path = Path(args.strategy_ledger)
    report_path = Path(args.reconciliation)
    audit_path = (
        Path(args.report_output) if args.report_output
        else registry_path.with_name(f"cancelled_beta_{args.order_id}.json")
    )
    try:
        if audit_path.exists():
            raise ValueError(f"resolution audit already exists: {audit_path}")
        registry_bytes = registry_path.read_bytes()
        ledger_bytes = ledger_path.read_bytes()
        report_bytes = report_path.read_bytes()
        registry = registry_from_payload(_object(json.loads(registry_bytes), "registry"))
        ledger = ledger_from_payload(_object(json.loads(ledger_bytes), "ledger"))
        reconciliation = _object(json.loads(report_bytes), "reconciliation")
        source = SimTradingPortfolioSource(
            args.base_url, access_token=os.getenv("SIM_ACCESS_TOKEN")
        )
        if not os.getenv("SIM_ACCESS_TOKEN"):
            username, password = os.getenv("SIM_USERNAME"), os.getenv("SIM_PASSWORD")
            if not username or not password:
                parser.error("set SIM_ACCESS_TOKEN or SIM_USERNAME and SIM_PASSWORD")
            source.login(username, password)
        updated, audit = verify_and_retire_cancelled_beta(
            source, registry, ledger, reconciliation, args.order_id
        )
        if (registry_path.read_bytes() != registry_bytes
                or ledger_path.read_bytes() != ledger_bytes
                or report_path.read_bytes() != report_bytes):
            raise ValueError("local registry, ledger, or reconciliation changed")
        # Audit first: a failed registry write leaves automatic trading blocked.
        _write(audit_path, audit)
        _write(registry_path, registry_to_payload(updated))
    except (ValueError, KeyError, OSError, json.JSONDecodeError, SimTradingError) as exc:
        raise SystemExit(f"Cancelled Beta recovery blocked: {exc}") from exc
    print(f"retired zero-fill cancelled Beta order {args.order_id}; orders submitted=0")
    print(f"wrote registry: {registry_path}")
    print(f"wrote resolution audit: {audit_path}")


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
