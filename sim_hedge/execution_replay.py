"""Replay recorded execution snapshots without market data or broker access."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Mapping

from execution_engine import (
    ConfirmedExecutionFill,
    WorkingExecutionOrder,
    assess_execution,
    start_execution_batch,
)


EXECUTION_REPLAY_VERSION = "sim-hedge/execution-replay/v1"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay an offline incremental hedge execution lifecycle"
    )
    parser.add_argument("scenario")
    parser.add_argument("--output", default="outputs/execution_replay.json")
    args = parser.parse_args()
    try:
        scenario = _object(_read(args.scenario), "execution replay scenario")
        result = replay_execution_scenario(scenario)
        _write(args.output, result)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Execution replay failed: {exc}") from exc
    for frame in result["frames"]:
        print(
            f"{frame['name']}: status={frame['status']} "
            f"uncovered={frame['uncovered_trades']}"
        )
    print(f"offline replay passed {len(result['frames'])} frames")
    print(f"wrote execution replay: {args.output}")


def replay_execution_scenario(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Assess independent cumulative snapshots and verify expected outcomes."""

    if scenario.get("protocol_version") != EXECUTION_REPLAY_VERSION:
        raise ValueError("unsupported execution replay protocol version")
    proposal = _object(scenario.get("hedge_proposal"), "hedge proposal")
    confirmed = _object(
        proposal.get("confirmed_beta_positions"), "confirmed Beta positions"
    )
    batch = start_execution_batch(
        proposal,
        pricing_request_id=str(proposal.get("source_pricing_request_id") or ""),
        account_id=str(proposal.get("account_id") or ""),
        ledger_revision=_integer(
            proposal.get("base_strategy_ledger_revision"), "base ledger revision"
        ),
        confirmed_beta_positions=confirmed,
    )
    raw_frames = scenario.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("execution replay frames must be a non-empty list")

    results = []
    names: set[str] = set()
    for raw_frame in raw_frames:
        frame = _object(raw_frame, "execution replay frame")
        name = str(frame.get("name") or "")
        if not name or name in names:
            raise ValueError("execution replay frame names must be unique and non-empty")
        names.add(name)
        fills = tuple(
            _fill(_object(item, "confirmed fill"), batch.proposal_id)
            for item in _list(frame.get("confirmed_fills", []), "confirmed fills")
        )
        working = tuple(
            _working(_object(item, "working order"), batch.proposal_id)
            for item in _list(frame.get("working_orders", []), "working orders")
        )
        assessment = assess_execution(
            batch,
            confirmed_fills=fills,
            working_orders=working,
        )
        expected_status = str(frame.get("expected_status") or "")
        if assessment.status != expected_status:
            raise ValueError(
                f"frame {name} expected {expected_status}, "
                f"received {assessment.status}"
            )
        expected_uncovered = frame.get("expected_uncovered_trades")
        if expected_uncovered is not None and dict(assessment.uncovered_trades) != dict(
            _object(expected_uncovered, "expected uncovered trades")
        ):
            raise ValueError(f"frame {name} has unexpected uncovered trades")
        results.append({"name": name, **asdict(assessment)})
    return {
        "protocol_version": EXECUTION_REPLAY_VERSION,
        "source_scenario": str(scenario.get("name") or ""),
        "proposal_id": batch.proposal_id,
        "broker_operations_performed": 0,
        "frames": results,
    }


def _fill(
    value: Mapping[str, Any], default_proposal_id: str
) -> ConfirmedExecutionFill:
    return ConfirmedExecutionFill(
        fill_id=str(value.get("fill_id") or ""),
        proposal_id=str(value.get("proposal_id") or default_proposal_id),
        instrument=str(value.get("instrument") or ""),
        quantity=_integer(value.get("quantity"), "fill quantity"),
    )


def _working(
    value: Mapping[str, Any], default_proposal_id: str
) -> WorkingExecutionOrder:
    return WorkingExecutionOrder(
        client_order_id=str(value.get("client_order_id") or ""),
        proposal_id=str(value.get("proposal_id") or default_proposal_id),
        instrument=str(value.get("instrument") or ""),
        remaining_quantity=_integer(
            value.get("remaining_quantity"), "working remaining quantity"
        ),
    )


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
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result != value:
        raise ValueError(f"{name} must be an integer")
    return result


if __name__ == "__main__":
    main()
