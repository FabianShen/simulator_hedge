"""Explicitly guarded ETF-option Alpha submission command."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from hedge_engine import (
    OrderRegistry,
    bind_broker_order,
    mark_submission_unknown,
)
from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
    SimTradingUnknownOutcomeError,
)
from sim_hedge.config import load_env_file
from sim_hedge.order_registry import registry_from_payload, registry_to_payload
from sim_hedge.portfolio import PortfolioSnapshot


Submit = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Persist = Callable[[OrderRegistry], None]


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Submit reviewed Alpha orders")
    parser.add_argument("pricing_request")
    parser.add_argument("alpha_order_dry_run")
    parser.add_argument("order_registry")
    parser.add_argument("--confirm-submit", required=True, metavar="ACCOUNT_ID")
    parser.add_argument("--max-total-contracts", required=True, type=int)
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--max-snapshot-age", type=float, default=10.0)
    parser.add_argument("--registry-output")
    parser.add_argument("--report-output", default="outputs/alpha_submission.json")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_output = args.registry_output or args.order_registry
    try:
        pricing = _object(_read(args.pricing_request), "pricing request")
        proposal = _object(_read(args.alpha_order_dry_run), "Alpha dry-run")
        registry = registry_from_payload(
            _object(_read(args.order_registry), "order registry")
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
        portfolio = source.load(registry.account_id)

        def persist(updated: OrderRegistry) -> None:
            _write(registry_output, registry_to_payload(updated))

        updated, report = submit_alpha_orders(
            pricing=pricing,
            proposal=proposal,
            registry=registry,
            portfolio=portfolio,
            confirmed_account_id=args.confirm_submit,
            submit=source.submit_etf_option_order,
            persist=persist,
            now=datetime.now(timezone.utc),
            max_snapshot_age_seconds=args.max_snapshot_age,
            max_total_contracts=args.max_total_contracts,
        )
        persist(updated)
        _write(args.report_output, report)
    except (ValueError, KeyError, json.JSONDecodeError, SimTradingError) as exc:
        raise SystemExit(f"Alpha submission stopped: {exc}") from exc
    print(
        f"submission accepted={len(report['accepted'])} "
        f"rejected={len(report['rejected'])} unknown={len(report['unknown'])}"
    )
    print(f"wrote order registry: {registry_output}")
    print(f"wrote submission report: {args.report_output}")
    if report["rejected"] or report["unknown"]:
        raise SystemExit(2)


def submit_alpha_orders(
    *,
    pricing: Mapping[str, Any],
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    portfolio: PortfolioSnapshot,
    confirmed_account_id: str,
    submit: Submit,
    persist: Persist,
    now: datetime,
    max_total_contracts: int,
    max_snapshot_age_seconds: float = 10.0,
) -> tuple[OrderRegistry, dict[str, Any]]:
    requests = _validate_submission(
        pricing,
        proposal,
        registry,
        portfolio,
        confirmed_account_id,
        now,
        max_snapshot_age_seconds,
        max_total_contracts,
    )
    accepted = []
    rejected = []
    unknown = []
    current = registry
    for request in requests:
        client_order_id = str(request["client_order_id"])
        try:
            response = submit(request)
        except SimTradingUnknownOutcomeError as exc:
            current = mark_submission_unknown(current, client_order_id)
            persist(current)
            unknown.append(
                {"client_order_id": client_order_id, "error": str(exc)}
            )
            break
        except SimTradingError as exc:
            rejected.append(
                {"client_order_id": client_order_id, "error": str(exc)}
            )
            break
        order_id = str(response["order_id"])
        current = bind_broker_order(
            current, client_order_id=client_order_id, order_id=order_id
        )
        persist(current)
        accepted.append(
            {"client_order_id": client_order_id, "order_id": order_id}
        )
    return current, {
        "source_pricing_request_id": pricing.get("requestId"),
        "account_id": registry.account_id,
        "accepted": accepted,
        "rejected": rejected,
        "unknown": unknown,
    }


def _validate_submission(
    pricing: Mapping[str, Any],
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    portfolio: PortfolioSnapshot,
    confirmed_account_id: str,
    now: datetime,
    max_age: float,
    max_total_contracts: int,
) -> tuple[Mapping[str, Any], ...]:
    if confirmed_account_id != registry.account_id:
        raise ValueError("--confirm-submit must exactly match the account ID")
    if now.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    if max_age <= 0:
        raise ValueError("max_snapshot_age_seconds must be positive")
    if max_total_contracts <= 0:
        raise ValueError("max_total_contracts must be positive")
    request_id = str(pricing.get("requestId") or "")
    if proposal.get("source_pricing_request_id") != request_id:
        raise ValueError("dry-run and pricing request IDs do not match")
    if proposal.get("submission_allowed") is not False:
        raise ValueError("expected a reviewed dry-run proposal")
    if proposal.get("orders_submitted") != 0:
        raise ValueError("dry-run already reports submitted orders")
    as_of = _datetime(str(pricing.get("asOf") or ""))
    age = (now - as_of).total_seconds()
    if age < 0 or age > max_age:
        raise ValueError(f"pricing snapshot is stale: age={age:g}s")
    if portfolio.account.account_id != registry.account_id:
        raise ValueError("portfolio and order registry account IDs do not match")
    if portfolio.account.status != "NORMAL":
        raise ValueError("broker account status is not NORMAL")
    if portfolio.account.risk_state not in (None, "NORMAL"):
        raise ValueError("broker account risk state is not NORMAL")
    if portfolio.positions:
        raise ValueError("Alpha submission requires an empty broker portfolio")
    if portfolio.active_orders:
        raise ValueError("Alpha submission requires no active broker orders")
    raw_requests = proposal.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValueError("dry-run requests must be a non-empty list")
    requests = tuple(_object(value, "dry-run request") for value in raw_requests)
    total_contracts = sum(int(value.get("volume") or 0) for value in requests)
    if total_contracts > max_total_contracts:
        raise ValueError(
            f"dry-run total volume {total_contracts} exceeds explicit limit "
            f"{max_total_contracts}"
        )
    underlying = _object(pricing.get("underlying"), "pricing underlying")
    _require_fresh(
        "underlying",
        underlying.get("observedAt"),
        now=now,
        max_age=max_age,
    )
    pricing_options = pricing.get("options")
    if not isinstance(pricing_options, list):
        raise ValueError("pricing options must be a list")
    option_times = {
        str(_object(value, "pricing option").get("instrument") or ""): value.get(
            "observedAt"
        )
        for value in pricing_options
    }
    client_ids = [str(value.get("client_order_id") or "") for value in requests]
    if len(client_ids) != len(set(client_ids)):
        raise ValueError("dry-run contains duplicate client_order_id values")
    for request, client_id in zip(requests, client_ids):
        intent = registry.intents.get(client_id)
        if intent is None:
            raise ValueError(f"unregistered client_order_id: {client_id}")
        _require_fresh(
            intent.instrument,
            option_times.get(intent.instrument),
            now=now,
            max_age=max_age,
        )
        if client_id in registry.broker_orders.values():
            raise ValueError(f"client_order_id already submitted: {client_id}")
        if client_id in registry.unknown_client_order_ids:
            raise ValueError(f"client_order_id has unknown submission state: {client_id}")
        expected = {
            "client_order_id": intent.client_order_id,
            "account_id": intent.account_id,
            "exchange_id": intent.exchange_id,
            "symbol": intent.instrument,
            "direction": "BUY" if intent.quantity > 0 else "SELL",
            "offset_flag": intent.offset,
            "order_type": intent.order_type,
            "limit_price": (
                None if intent.limit_price is None else str(intent.limit_price)
            ),
            "volume": abs(intent.quantity),
        }
        if dict(request) != expected:
            raise ValueError(f"dry-run request conflicts with registry: {client_id}")
    return requests


def _require_fresh(name: str, value: Any, *, now: datetime, max_age: float) -> None:
    observed = _datetime(str(value or ""))
    age = (now - observed).total_seconds()
    if age < 0 or age > max_age:
        raise ValueError(f"{name} market observation is stale: age={age:g}s")


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


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("pricing asOf must be timezone-aware")
    return result


if __name__ == "__main__":
    main()
