"""Freeze and inspect one hedge execution batch without changing broker state."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Mapping

from execution_engine import ExecutionAssessment, start_execution_batch
from sim_hedge.adapters.sim_trading import SimTradingError, SimTradingPortfolioSource
from sim_hedge.config import load_env_file
from sim_hedge.state.execution_state import assess_broker_execution
from sim_hedge.state.registry import registry_from_payload
from sim_hedge.state.ledger import ledger_from_payload


EXECUTION_BATCH_VERSION = "sim-hedge/execution-batch/v1"


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Accept or inspect one incremental hedge execution batch"
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    accept = subparsers.add_parser("accept", help="freeze one reviewed proposal")
    accept.add_argument("proposal")
    accept.add_argument("strategy_ledger")
    accept.add_argument("--output", required=True)

    status = subparsers.add_parser(
        "status", help="read broker state and assess the frozen proposal"
    )
    status.add_argument("accepted_proposal")
    status.add_argument("order_registry")
    status.add_argument("strategy_ledger")
    status.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    status.add_argument("--output", default="outputs/execution_status.json")

    args = parser.parse_args()
    try:
        if args.operation == "accept":
            proposal = _object(_read(args.proposal), "hedge proposal")
            ledger = ledger_from_payload(
                _object(_read(args.strategy_ledger), "strategy ledger")
            )
            payload = build_accepted_execution_batch(proposal, ledger)
            created = _write_once(args.output, payload)
            verb = "accepted" if created else "already accepted"
            print(f"{verb} hedge proposal {payload['proposal_id']}")
            print(f"wrote immutable execution batch: {args.output}")
            return

        if not args.base_url:
            parser.error("set SIM_REST_BASE_URL or pass --base-url")
        accepted = _object(_read(args.accepted_proposal), "execution batch")
        proposal = proposal_from_accepted_batch(accepted)
        registry = registry_from_payload(
            _object(_read(args.order_registry), "order registry")
        )
        ledger = ledger_from_payload(
            _object(_read(args.strategy_ledger), "strategy ledger")
        )
        source = _portfolio_source(args.base_url, parser)
        portfolio = source.load(ledger.account_id)
        assessment = assess_broker_execution(
            proposal,
            ledger=ledger,
            registry=registry,
            portfolio=portfolio,
        )
        payload = assessment_to_payload(assessment)
        _write(args.output, payload)
        print(
            f"execution proposal={assessment.proposal_id} "
            f"status={assessment.status} "
            f"uncovered={dict(assessment.uncovered_trades)}"
        )
        if assessment.cancellation_candidate_ids:
            print(
                "cancellation candidates (not cancelled): "
                + ", ".join(assessment.cancellation_candidate_ids)
            )
        print(f"wrote read-only execution status: {args.output}")
    except (ValueError, KeyError, json.JSONDecodeError, SimTradingError) as exc:
        raise SystemExit(f"Execution monitor failed: {exc}") from exc


def build_accepted_execution_batch(proposal: Mapping[str, Any], ledger) -> dict[str, Any]:
    """Validate one proposal and produce a persistent, non-trading record."""

    start_execution_batch(
        proposal,
        pricing_request_id=str(proposal.get("source_pricing_request_id") or ""),
        account_id=ledger.account_id,
        ledger_revision=ledger.revision,
        confirmed_beta_positions=ledger.beta_positions,
    )
    return {
        **dict(proposal),
        "execution_batch_version": EXECUTION_BATCH_VERSION,
        "submission_allowed": False,
    }


def proposal_from_accepted_batch(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("execution_batch_version") != EXECUTION_BATCH_VERSION:
        raise ValueError("unsupported execution batch protocol version")
    if payload.get("submission_allowed") is not False:
        raise ValueError("execution batch must not grant submission permission")
    return payload


def assessment_to_payload(value: ExecutionAssessment) -> dict[str, Any]:
    return {
        "execution_batch_version": EXECUTION_BATCH_VERSION,
        "submission_allowed": False,
        "broker_operations_performed": 0,
        **asdict(value),
    }


def _portfolio_source(base_url: str, parser: argparse.ArgumentParser):
    token = os.getenv("SIM_ACCESS_TOKEN")
    source = SimTradingPortfolioSource(base_url, access_token=token)
    if token:
        return source
    username = os.getenv("SIM_USERNAME")
    password = os.getenv("SIM_PASSWORD")
    if not username or not password:
        parser.error("set SIM_ACCESS_TOKEN, or set SIM_USERNAME and SIM_PASSWORD")
    source.login(username, password)
    return source


def _write_once(path: str, payload: Mapping[str, Any]) -> bool:
    destination = Path(path)
    if destination.exists():
        existing = _object(_read(destination), "existing execution batch")
        if existing != payload:
            raise ValueError(
                "accepted execution batch already exists with different contents"
            )
        return False
    _write(path, payload)
    return True


def _read(path: str | Path) -> Any:
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
