"""Submit a validated ETF-option Beta hedge batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    OrderRegistry,
    StrategyLedger,
    combined_strategy_positions,
    strategy_intents_fully_filled,
)
from sim_hedge.adapters.sim_trading import SimTradingError, SimTradingPortfolioSource
from sim_hedge.config import load_env_file
from sim_hedge.order_registry import registry_from_payload, registry_to_payload
from sim_hedge.order_submission import (
    Persist,
    Submit,
    execute_registered_requests,
    validate_registered_requests,
    validate_timestamp_freshness,
)
from sim_hedge.portfolio import PortfolioSnapshot
from sim_hedge.strategy_ledger import ledger_from_payload


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Submit validated Beta hedge orders")
    parser.add_argument("beta_order_dry_run")
    parser.add_argument("order_registry")
    parser.add_argument("strategy_ledger")
    parser.add_argument("--max-total-contracts", required=True, type=int)
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--max-snapshot-age", type=float, default=10.0)
    parser.add_argument("--registry-output")
    parser.add_argument("--report-output", default="outputs/beta_submission.json")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    registry_output = args.registry_output or args.order_registry
    try:
        proposal = _object(_read(args.beta_order_dry_run), "Beta dry-run")
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
        portfolio = source.load(registry.account_id)

        def persist(updated: OrderRegistry) -> None:
            _write(registry_output, registry_to_payload(updated))

        updated, report = submit_beta_orders(
            proposal=proposal,
            registry=registry,
            ledger=ledger,
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
        raise SystemExit(f"Beta submission stopped: {exc}") from exc
    print(
        f"Beta submission accepted={len(report['accepted'])} "
        f"rejected={len(report['rejected'])} unknown={len(report['unknown'])}"
    )
    print(f"wrote order registry: {registry_output}")
    print(f"wrote submission report: {args.report_output}")
    if report["rejected"] or report["unknown"]:
        raise SystemExit(2)


def submit_beta_orders(
    *,
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    ledger: StrategyLedger,
    portfolio: PortfolioSnapshot,
    submit: Submit,
    persist: Persist,
    now: datetime,
    max_total_contracts: int,
    max_snapshot_age_seconds: float = 10.0,
) -> tuple[OrderRegistry, dict[str, Any]]:
    requests = _validate_beta_submission(
        proposal,
        registry,
        ledger,
        portfolio,
        now,
        max_snapshot_age_seconds,
        max_total_contracts,
    )
    persist(registry)
    updated, outcomes = execute_registered_requests(
        registry, requests, submit=submit, persist=persist
    )
    return updated, {
        "source_hedge_proposal_id": proposal.get("source_hedge_proposal_id"),
        "source_pricing_request_id": proposal.get("source_pricing_request_id"),
        "base_strategy_ledger_revision": ledger.revision,
        "account_id": registry.account_id,
        **outcomes,
    }


def _validate_beta_submission(
    proposal: Mapping[str, Any],
    registry: OrderRegistry,
    ledger: StrategyLedger,
    portfolio: PortfolioSnapshot,
    now: datetime,
    max_age: float,
    max_total_contracts: int,
) -> tuple[Mapping[str, Any], ...]:
    if ledger.account_id != registry.account_id:
        raise ValueError("strategy ledger and order registry accounts do not match")
    if portfolio.account.account_id != registry.account_id:
        raise ValueError("portfolio and order registry account IDs do not match")
    if not proposal.get("source_hedge_proposal_id"):
        raise ValueError("dry-run is missing its source hedge proposal ID")
    if proposal.get("base_strategy_ledger_revision") != ledger.revision:
        raise ValueError("dry-run and strategy ledger revisions do not match")
    if proposal.get("strategy") != "BETA":
        raise ValueError("dry-run strategy must be BETA")
    if proposal.get("submission_allowed") is not False:
        raise ValueError("expected a reviewed dry-run proposal")
    if proposal.get("orders_submitted") != 0:
        raise ValueError("dry-run already reports submitted orders")
    if proposal.get("order_type") != "COUNTERPARTY":
        raise ValueError("Beta dry-run order type must be COUNTERPARTY")
    if portfolio.account.status != "NORMAL":
        raise ValueError("broker account status is not NORMAL")
    if portfolio.account.risk_state not in (None, "NORMAL"):
        raise ValueError("broker account risk state is not NORMAL")
    if portfolio.active_orders:
        raise ValueError("Beta submission requires no active broker orders")
    if registry.unknown_client_order_ids:
        raise ValueError("order registry contains unknown submission outcomes")
    if not strategy_intents_fully_filled(registry, ledger, "ALPHA"):
        raise ValueError("Alpha intents are not fully confirmed")
    if portfolio.signed_positions != combined_strategy_positions(ledger):
        raise ValueError("broker positions do not match the confirmed strategy ledger")
    requests = validate_registered_requests(
        proposal.get("requests"),
        registry,
        strategy="BETA",
        max_total_contracts=max_total_contracts,
    )
    request_clients = {str(request["client_order_id"]) for request in requests}
    bound_clients = set(registry.broker_orders.values())
    active_intents = set(registry.intents) - set(
        registry.superseded_client_order_ids
    ) - set(registry.abandoned_client_order_ids)
    if active_intents - bound_clients != request_clients:
        raise ValueError("registry unbound intents do not exactly match the Beta dry-run")
    validate_timestamp_freshness(
        "hedge source market snapshot",
        proposal.get("source_market_as_of"),
        now=now,
        max_age=max_age,
    )
    return requests


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
