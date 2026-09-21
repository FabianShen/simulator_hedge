"""Read broker facts and reconcile Alpha order ownership and positions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import DecimalException
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from hedge_engine import (
    ConfirmedFill,
    OrderRegistry,
    OrderIntent,
    StrategyLedger,
    all_active_intents_fully_filled,
    apply_confirmed_fills,
    bind_broker_order,
    empty_ledger,
    combined_strategy_positions,
    strategy_intents_fully_filled,
)
from sim_hedge.adapters.sim_trading import (
    BrokerOrder,
    SimTradingError,
    SimTradingPortfolioSource,
)
from sim_hedge.config import load_env_file
from sim_hedge.order_registry import registry_from_payload, registry_to_payload
from sim_hedge.portfolio import PortfolioSnapshot
from sim_hedge.strategy_ledger import ledger_from_payload, ledger_to_payload


@dataclass(frozen=True)
class ReconciliationResult:
    registry: OrderRegistry
    ledger: StrategyLedger
    report: Mapping[str, Any]


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Read broker orders/trades and reconcile strategy state"
    )
    parser.add_argument("order_registry")
    parser.add_argument("--ledger", default="outputs/strategy_ledger.json")
    parser.add_argument(
        "--initialize-ledger",
        action="store_true",
        help="rebuild the first confirmed ledger from broker trades",
    )
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--registry-output")
    parser.add_argument("--ledger-output")
    parser.add_argument(
        "--report-output", default="outputs/portfolio_reconciliation.json"
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_output = args.registry_output or args.order_registry
    ledger_output = args.ledger_output or args.ledger
    try:
        registry = registry_from_payload(_object(_read(args.order_registry), "registry"))
        ledger_path = Path(args.ledger)
        if args.initialize_ledger:
            ledger = empty_ledger(registry.account_id)
        elif ledger_path.is_file():
            ledger = ledger_from_payload(
                _object(_read(ledger_path), "strategy ledger")
            )
        else:
            raise ValueError(
                "confirmed strategy ledger does not exist; "
                "pass --initialize-ledger for the first reconciliation"
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

        initial_portfolio = source.load(registry.account_id)
        trading_day = initial_portfolio.account.trading_day
        if trading_day is None:
            raise ValueError("broker snapshot has no trading_day")
        orders = _load_all_orders(source, registry.account_id, trading_day)
        recovered_registry = recover_order_bindings(registry, orders)
        fills = _load_all_fills(source, recovered_registry)
        portfolio = source.load(registry.account_id)
        result = reconcile_alpha_state(
            registry=recovered_registry,
            ledger=ledger,
            fills=fills,
            orders=orders,
            portfolio=portfolio,
            previous_registry=registry,
        )
        _write(registry_output, registry_to_payload(result.registry))
        _write(ledger_output, ledger_to_payload(result.ledger))
        _write(args.report_output, result.report)
    except (
        ValueError,
        KeyError,
        DecimalException,
        json.JSONDecodeError,
        SimTradingError,
    ) as exc:
        raise SystemExit(f"Portfolio reconciliation failed: {exc}") from exc

    print(
        f"portfolio reconciliation safe_for_hedging="
        f"{result.report['safe_for_hedging']} "
        f"recovered={len(result.report['recovered_orders'])} "
        f"fills={len(result.ledger.applied_trades)} "
        f"position_match={result.report['position_match']}"
    )
    if result.report["unresolved_submissions"]:
        print(
            "unresolved submissions: "
            + ", ".join(result.report["unresolved_submissions"])
        )
    print(f"wrote order registry: {registry_output}")
    print(f"wrote strategy ledger: {ledger_output}")
    print(f"wrote reconciliation report: {args.report_output}")
    if not result.report["safe_for_hedging"]:
        raise SystemExit(2)


def recover_order_bindings(
    registry: OrderRegistry, orders: Sequence[BrokerOrder]
) -> OrderRegistry:
    """Bind only exact broker matches for intents already owned by this process."""

    current = registry
    seen_clients: dict[str, str] = {}
    for order in orders:
        intent = current.intents.get(order.client_order_id)
        if intent is None:
            continue
        previous_order_id = seen_clients.get(order.client_order_id)
        if previous_order_id is not None and previous_order_id != order.order_id:
            raise ValueError(
                f"multiple broker orders use client_order_id {order.client_order_id}"
            )
        seen_clients[order.client_order_id] = order.order_id
        _verify_order_matches_intent(order, intent)
        current = bind_broker_order(
            current,
            client_order_id=order.client_order_id,
            order_id=order.order_id,
        )
    return current


def reconcile_alpha_state(
    *,
    registry: OrderRegistry,
    ledger: StrategyLedger,
    fills: Sequence[ConfirmedFill],
    orders: Sequence[BrokerOrder],
    portfolio: PortfolioSnapshot,
    previous_registry: OrderRegistry,
) -> ReconciliationResult:
    if ledger.account_id != registry.account_id:
        raise ValueError("strategy ledger and order registry accounts do not match")
    if portfolio.account.account_id != registry.account_id:
        raise ValueError("broker portfolio and order registry accounts do not match")
    updated_ledger = apply_confirmed_fills(ledger, fills)
    broker_positions = portfolio.signed_positions
    strategy_positions = combined_strategy_positions(updated_ledger)
    position_match = broker_positions == strategy_positions
    managed_orders = {
        order.order_id: order
        for order in orders
        if order.order_id in registry.broker_orders
    }
    active_order_ids = sorted(order.order_id for order in portfolio.active_orders)
    bound_clients = set(registry.broker_orders.values())
    unbound_intents = sorted(
        client_id
        for client_id, intent in registry.intents.items()
        if client_id not in bound_clients
        and client_id not in registry.superseded_client_order_ids
        and client_id not in registry.abandoned_client_order_ids
    )
    recovered = sorted(
        client_id
        for client_id in registry.broker_orders.values()
        if client_id not in previous_registry.broker_orders.values()
    )
    alpha_fill_complete = strategy_intents_fully_filled(
        registry, updated_ledger, "ALPHA"
    )
    has_beta_intents = any(
        intent.strategy == "BETA"
        and intent.client_order_id not in registry.superseded_client_order_ids
        for intent in registry.intents.values()
    )
    beta_fill_complete = (
        strategy_intents_fully_filled(registry, updated_ledger, "BETA")
        if has_beta_intents
        else None
    )
    all_fills_complete = all_active_intents_fully_filled(
        registry, updated_ledger
    )
    account_healthy = (
        portfolio.account.status == "NORMAL"
        and portfolio.account.risk_state in (None, "NORMAL")
    )
    safe = (
        not registry.unknown_client_order_ids
        and not unbound_intents
        and not active_order_ids
        and position_match
        and all_fills_complete
        and account_healthy
    )
    report = {
        "status": "RECONCILED" if safe else "NOT_READY",
        "account_id": registry.account_id,
        "safe_for_hedging": safe,
        "registry_revision": registry.revision,
        "ledger_revision": updated_ledger.revision,
        "recovered_orders": recovered,
        "unresolved_submissions": list(registry.unknown_client_order_ids),
        "unbound_order_intents": unbound_intents,
        "active_order_ids": active_order_ids,
        "account_healthy": account_healthy,
        "alpha_fill_complete": alpha_fill_complete,
        "beta_fill_complete": beta_fill_complete,
        "all_fills_complete": all_fills_complete,
        "position_match": position_match,
        "broker_positions": broker_positions,
        "strategy_positions": strategy_positions,
        "managed_order_statuses": {
            order_id: order.status for order_id, order in sorted(managed_orders.items())
        },
        "applied_trade_ids": sorted(updated_ledger.applied_trades),
    }
    return ReconciliationResult(registry, updated_ledger, report)


def _verify_order_matches_intent(order: BrokerOrder, intent: OrderIntent) -> None:
    expected_direction = "BUY" if intent.quantity > 0 else "SELL"
    expected = (
        intent.account_id,
        intent.exchange_id,
        intent.instrument,
        expected_direction,
        intent.offset,
        intent.order_type,
        intent.limit_price,
        abs(intent.quantity),
    )
    actual = (
        order.account_id,
        order.exchange_id,
        order.instrument,
        order.direction,
        order.offset,
        order.order_type,
        order.limit_price,
        order.total_volume,
    )
    if actual != expected:
        raise ValueError(
            f"broker order {order.order_id} conflicts with registered intent "
            f"{intent.client_order_id}"
        )


def _load_all_orders(
    source: SimTradingPortfolioSource, account_id: str, trading_day: date
) -> tuple[BrokerOrder, ...]:
    orders: list[BrokerOrder] = []
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        page = source.load_order_page(
            account_id, trading_day, cursor=cursor, limit=100
        )
        orders.extend(page.orders)
        if not page.has_more:
            return tuple(orders)
        if page.next_cursor is None or page.next_cursor in seen_cursors:
            raise SimTradingError("order pagination cursor did not advance")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _load_all_fills(
    source: SimTradingPortfolioSource, registry: OrderRegistry
) -> tuple[ConfirmedFill, ...]:
    fills: list[ConfirmedFill] = []
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        page = source.load_confirmed_trade_page(
            registry.account_id,
            registry.order_strategies,
            cursor=cursor,
            limit=100,
        )
        fills.extend(page.fills)
        if not page.has_more:
            return tuple(fills)
        if page.next_cursor is None or page.next_cursor in seen_cursors:
            raise SimTradingError("trade pagination cursor did not advance")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str | Path, payload: Mapping[str, Any]) -> None:
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
