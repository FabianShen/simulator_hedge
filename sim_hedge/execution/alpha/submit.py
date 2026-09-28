"""Submit a validated ETF-option Alpha batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Any, Mapping

from hedge_engine import OrderRegistry
from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
)
from sim_hedge.config import load_env_file
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.execution.submission import (
    Persist,
    Submit,
    execute_registered_requests,
    validate_timestamp_freshness,
    validate_registered_requests,
)
from sim_hedge.domain.portfolio import PortfolioSnapshot
from sim_hedge.jsonio import (
    read_json as _read,
    require_object as _object,
    write_json as _write,
)


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Submit validated Alpha orders")
    parser.add_argument("alpha_order_dry_run")
    parser.add_argument("order_registry")
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
            proposal=proposal,
            registry=registry,
            portfolio=portfolio,
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
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    portfolio: PortfolioSnapshot,
    submit: Submit,
    persist: Persist,
    now: datetime,
    max_total_contracts: int,
    max_snapshot_age_seconds: float = 10.0,
) -> tuple[OrderRegistry, dict[str, Any]]:
    requests = _validate_submission(
        proposal,
        registry,
        portfolio,
        now,
        max_snapshot_age_seconds,
        max_total_contracts,
    )
    # Record ownership before the first POST, but only after every preflight check.
    persist(registry)
    current, outcomes = execute_registered_requests(
        registry, requests, submit=submit, persist=persist
    )
    return current, {
        "source_alpha_market_id": proposal.get("source_alpha_market_id"),
        "account_id": registry.account_id,
        **outcomes,
    }


def _validate_submission(
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    portfolio: PortfolioSnapshot,
    now: datetime,
    max_age: float,
    max_total_contracts: int,
) -> tuple[Mapping[str, Any], ...]:
    if not proposal.get("source_alpha_market_id"):
        raise ValueError("dry-run is missing its Alpha market snapshot ID")
    if proposal.get("submission_allowed") is not False:
        raise ValueError("expected a reviewed dry-run proposal")
    if proposal.get("orders_submitted") != 0:
        raise ValueError("dry-run already reports submitted orders")
    if proposal.get("order_type") != "COUNTERPARTY":
        raise ValueError("Alpha dry-run order type must be COUNTERPARTY")
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
    requests = validate_registered_requests(
        proposal.get("requests"),
        registry,
        strategy="ALPHA",
        max_total_contracts=max_total_contracts,
    )
    validate_timestamp_freshness(
        "Alpha source market snapshot",
        proposal.get("source_market_as_of"),
        now=now,
        max_age=max_age,
    )
    return requests


if __name__ == "__main__":
    main()
