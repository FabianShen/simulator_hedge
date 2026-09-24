"""Broker-verified, no-trade resolution of one unknown Alpha submission."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from hedge_engine import (
    OrderRegistry, StrategyLedger, combined_strategy_positions,
    retire_verified_absent_submission,
)
from sim_hedge.adapters.sim_trading import SimTradingError, SimTradingPortfolioSource
from sim_hedge.execution.alpha.reconcile import load_all_orders
from sim_hedge.config import load_env_file
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.state.ledger import ledger_from_payload


def verify_and_retire(
    source: SimTradingPortfolioSource,
    registry: OrderRegistry,
    ledger: StrategyLedger,
    client_order_id: str,
    submission_report: Mapping[str, Any],
) -> tuple[OrderRegistry, dict[str, Any]]:
    """Read the complete broker history twice; never submit or cancel an order."""

    intent = registry.intents.get(client_order_id)
    if intent is None or intent.strategy != "ALPHA":
        raise ValueError("client_order_id is not a registered Alpha intent")
    if client_order_id not in registry.unknown_client_order_ids:
        raise ValueError("Alpha intent does not have an unknown outcome")
    if registry.account_id != ledger.account_id:
        raise ValueError("registry and ledger accounts do not match")
    _verify_preparing_response(submission_report, registry.account_id, client_order_id)
    if intent.created_at.tzinfo is None:
        raise ValueError("intent creation time must be timezone-aware")
    trading_day = intent.created_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    first = source.load(registry.account_id)
    _check_portfolio(first, registry, ledger, trading_day)

    orders_before = load_all_orders(source, registry.account_id, trading_day)
    trade_order_ids = _all_trade_order_ids(source, registry.account_id)
    orders_after = load_all_orders(source, registry.account_id, trading_day)
    trades_after = _all_trade_order_ids(source, registry.account_id)
    second = source.load(registry.account_id)
    _check_portfolio(second, registry, ledger, trading_day)
    if first.signed_positions != second.signed_positions:
        raise ValueError("broker positions changed during verification")

    for orders in (orders_before, orders_after):
        matches = [order for order in orders if order.client_order_id == client_order_id]
        if matches:
            raise ValueError("broker order exists for unknown intent; reconcile instead")
        unregistered = set(order.order_id for order in orders) - set(registry.broker_orders)
        if unregistered:
            raise ValueError("broker history contains orders outside the registry")
    if {order.order_id for order in orders_before} != {order.order_id for order in orders_after}:
        raise ValueError("broker order history changed during verification")
    if trade_order_ids != trades_after:
        raise ValueError("broker trade history changed during verification")
    unowned_trades = trade_order_ids - set(registry.broker_orders)
    if unowned_trades:
        raise ValueError("broker has trades for orders outside the registry")

    updated = retire_verified_absent_submission(registry, client_order_id)
    audit = {
        "version": "sim-hedge/unknown-alpha-resolution/v1",
        "account_id": registry.account_id,
        "client_order_id": client_order_id,
        "instrument": intent.instrument,
        "trading_day": trading_day.isoformat(),
        "source_registry_revision": registry.revision,
        "registry_revision": updated.revision,
        "ledger_revision": ledger.revision,
        "checked_broker_orders": len(orders_after),
        "checked_trade_order_ids": len(trade_order_ids),
        "broker_positions": dict(sorted(second.signed_positions.items())),
        "decision": "RETIRED_VERIFIED_ABSENT",
        "orders_submitted": 0,
    }
    return updated, audit


def _verify_preparing_response(report: Mapping[str, Any], account_id: str, client_id: str) -> None:
    if report.get("account_id") != account_id:
        raise ValueError("submission report account does not match")
    unknown = report.get("unknown")
    if not isinstance(unknown, list) or len(unknown) != 1:
        raise ValueError("submission report must identify exactly one unknown outcome")
    entry = unknown[0]
    if not isinstance(entry, Mapping) or entry.get("client_order_id") != client_id:
        raise ValueError("submission report does not match the unknown intent")
    error = str(entry.get("error") or "")
    if not error.startswith("simulator HTTP 503;") or "{" not in error:
        raise ValueError("unknown outcome is not a documented market-data-preparing response")
    try:
        response = json.loads(error[error.index("{"):])
    except json.JSONDecodeError as exc:
        raise ValueError("simulator response in submission report is invalid") from exc
    if not isinstance(response, Mapping) or response.get("error_code") != "ORDER_MARKET_DATA_PREPARING":
        raise ValueError("unknown outcome is not ORDER_MARKET_DATA_PREPARING")


def _check_portfolio(portfolio, registry, ledger, trading_day) -> None:
    account = portfolio.account
    if account.account_id != registry.account_id or account.trading_day != trading_day:
        raise ValueError("broker account or trading day differs from the unknown intent")
    if account.status != "NORMAL" or account.risk_state not in (None, "NORMAL"):
        raise ValueError("broker account is not healthy")
    if portfolio.active_orders:
        raise ValueError("broker still has active orders")
    if portfolio.signed_positions != combined_strategy_positions(ledger):
        raise ValueError("broker positions differ from the confirmed ledger")


def _all_trade_order_ids(source, account_id: str) -> set[str]:
    result: set[str] = set()
    cursor = None
    seen: set[str] = set()
    while True:
        page = source.load_trade_order_ids_page(account_id, cursor=cursor, limit=100)
        result.update(page.order_ids)
        if not page.has_more:
            return result
        if page.next_cursor is None or page.next_cursor in seen:
            raise SimTradingError("trade pagination cursor did not advance")
        seen.add(page.next_cursor)
        cursor = page.next_cursor


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Retire one broker-verified absent Alpha submission; no orders sent")
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("submission_report", help="recorded Alpha submission outcome")
    parser.add_argument("--client-order-id", required=True)
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--report-output")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_path = Path(args.order_registry)
    ledger_path = Path(args.strategy_ledger)
    report_path = Path(args.report_output) if args.report_output else registry_path.with_name("unknown_alpha_resolution.json")
    try:
        if report_path.exists():
            raise ValueError(f"resolution report already exists: {report_path}")
        registry_bytes = registry_path.read_bytes()
        ledger_bytes = ledger_path.read_bytes()
        registry = registry_from_payload(_object(json.loads(registry_bytes), "registry"))
        ledger = ledger_from_payload(_object(json.loads(ledger_bytes), "ledger"))
        submission = _object(json.loads(Path(args.submission_report).read_text(encoding="utf-8")), "submission report")
        source = SimTradingPortfolioSource(
            args.base_url, access_token=os.getenv("SIM_ACCESS_TOKEN")
        )
        if not os.getenv("SIM_ACCESS_TOKEN"):
            username, password = os.getenv("SIM_USERNAME"), os.getenv("SIM_PASSWORD")
            if not username or not password:
                parser.error("set SIM_ACCESS_TOKEN or SIM_USERNAME and SIM_PASSWORD")
            source.login(username, password)
        updated, audit = verify_and_retire(
            source, registry, ledger, args.client_order_id, submission
        )
        if registry_path.read_bytes() != registry_bytes or ledger_path.read_bytes() != ledger_bytes:
            raise ValueError("local registry or ledger changed during verification")
        # Record the proof first; a failed registry write leaves trading blocked.
        _write(report_path, audit)
        _write(registry_path, registry_to_payload(updated))
    except (ValueError, KeyError, OSError, json.JSONDecodeError, SimTradingError) as exc:
        raise SystemExit(f"Unknown Alpha resolution blocked: {exc}") from exc
    print(f"retired verified-absent Alpha intent {args.client_order_id}; orders submitted=0")
    print(f"wrote registry: {registry_path}")
    print(f"wrote resolution audit: {report_path}")


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
