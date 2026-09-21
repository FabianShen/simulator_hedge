"""Offline progress model for one immutable incremental hedge proposal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hedge_engine import validate_hedge_proposal


@dataclass(frozen=True)
class ExecutionBatch:
    """An accepted hedge proposal frozen against its original ledger state."""

    proposal_id: str
    account_id: str
    base_strategy_ledger_revision: int
    requested_trades: Mapping[str, int]


@dataclass(frozen=True)
class ConfirmedExecutionFill:
    """A broker-confirmed signed fill attributed to one proposal."""

    fill_id: str
    proposal_id: str
    instrument: str
    quantity: int


@dataclass(frozen=True)
class WorkingExecutionOrder:
    """The signed unfilled remainder of a broker order."""

    client_order_id: str
    proposal_id: str
    instrument: str
    remaining_quantity: int


@dataclass(frozen=True)
class ExecutionAssessment:
    """Read-only execution decision; it contains no broker command."""

    proposal_id: str
    status: str
    requested_trades: Mapping[str, int]
    confirmed_fills: Mapping[str, int]
    working_trades: Mapping[str, int]
    uncovered_trades: Mapping[str, int]
    cancellation_candidate_ids: tuple[str, ...]
    blockers: tuple[str, ...]


def start_execution_batch(
    proposal: Mapping[str, Any],
    *,
    pricing_request_id: str,
    account_id: str,
    ledger_revision: int,
    confirmed_beta_positions: Mapping[str, int],
) -> ExecutionBatch:
    """Validate and freeze an incremental proposal before any order is created."""

    trades = validate_hedge_proposal(
        proposal,
        pricing_request_id=pricing_request_id,
        account_id=account_id,
        base_ledger_revision=ledger_revision,
        confirmed_beta_positions=confirmed_beta_positions,
    )
    return ExecutionBatch(
        proposal_id=str(proposal["proposal_id"]),
        account_id=account_id,
        base_strategy_ledger_revision=ledger_revision,
        requested_trades=dict(trades),
    )


def assess_execution(
    batch: ExecutionBatch,
    *,
    confirmed_fills: Sequence[ConfirmedExecutionFill] = (),
    working_orders: Sequence[WorkingExecutionOrder] = (),
) -> ExecutionAssessment:
    """Calculate proposal-relative progress without submitting or cancelling.

    ``target_beta_positions`` deliberately does not enter this calculation. The
    executable signal is the batch's frozen incremental trade. A working order
    owned by another proposal blocks this batch so that successive live hedge
    proposals cannot accidentally duplicate one another.
    """

    requested = _positions(batch.requested_trades, "requested trade")
    fills = _aggregate_fills(batch, confirmed_fills)
    owned_working, foreign = _aggregate_working(batch, working_orders)
    _validate_progress(requested, fills, owned_working)

    uncovered = _subtract(requested, fills, owned_working)
    cancellation_ids = tuple(sorted(order.client_order_id for order in foreign))
    if foreign:
        owners = sorted({order.proposal_id for order in foreign})
        return ExecutionAssessment(
            proposal_id=batch.proposal_id,
            status="BLOCKED_BY_PRIOR_PROPOSAL",
            requested_trades=requested,
            confirmed_fills=fills,
            working_trades=owned_working,
            uncovered_trades={},
            cancellation_candidate_ids=cancellation_ids,
            blockers=(
                "working orders belong to proposal(s): " + ", ".join(owners),
            ),
        )

    if not requested:
        status = "NO_ACTION"
    elif uncovered:
        status = "READY_TO_SUBMIT"
    elif owned_working:
        status = "WORKING"
    else:
        status = "COMPLETE"
    return ExecutionAssessment(
        proposal_id=batch.proposal_id,
        status=status,
        requested_trades=requested,
        confirmed_fills=fills,
        working_trades=owned_working,
        uncovered_trades=uncovered,
        cancellation_candidate_ids=(),
        blockers=(),
    )


def _aggregate_fills(
    batch: ExecutionBatch, fills: Sequence[ConfirmedExecutionFill]
) -> dict[str, int]:
    seen: set[str] = set()
    result: dict[str, int] = {}
    for fill in fills:
        if not fill.fill_id or fill.fill_id in seen:
            raise ValueError("confirmed execution fill IDs must be unique and non-empty")
        seen.add(fill.fill_id)
        if fill.proposal_id != batch.proposal_id:
            raise ValueError("confirmed fill belongs to a different hedge proposal")
        _add(result, fill.instrument, fill.quantity, "confirmed fill")
    return dict(sorted(result.items()))


def _aggregate_working(
    batch: ExecutionBatch, orders: Sequence[WorkingExecutionOrder]
) -> tuple[dict[str, int], tuple[WorkingExecutionOrder, ...]]:
    seen: set[str] = set()
    owned: dict[str, int] = {}
    foreign: list[WorkingExecutionOrder] = []
    for order in orders:
        if not order.client_order_id or order.client_order_id in seen:
            raise ValueError("working client order IDs must be unique and non-empty")
        seen.add(order.client_order_id)
        if not order.proposal_id:
            raise ValueError("working order proposal ID must not be empty")
        if order.remaining_quantity == 0:
            raise ValueError("working order remaining quantity must not be zero")
        if order.proposal_id == batch.proposal_id:
            _add(
                owned,
                order.instrument,
                order.remaining_quantity,
                "working order",
            )
        else:
            foreign.append(order)
    return dict(sorted(owned.items())), tuple(foreign)


def _validate_progress(
    requested: Mapping[str, int],
    fills: Mapping[str, int],
    working: Mapping[str, int],
) -> None:
    for source_name, source in (("fill", fills), ("working order", working)):
        for instrument, quantity in source.items():
            target = requested.get(instrument)
            if target is None:
                raise ValueError(f"{source_name} {instrument} is not in the proposal")
            if (quantity > 0) != (target > 0):
                raise ValueError(f"{source_name} {instrument} has the wrong direction")
    for instrument, target in requested.items():
        progressed = abs(fills.get(instrument, 0)) + abs(working.get(instrument, 0))
        if progressed > abs(target):
            raise ValueError(f"execution progress exceeds requested trade for {instrument}")


def _subtract(
    requested: Mapping[str, int],
    fills: Mapping[str, int],
    working: Mapping[str, int],
) -> dict[str, int]:
    result = {
        instrument: quantity
        - fills.get(instrument, 0)
        - working.get(instrument, 0)
        for instrument, quantity in requested.items()
    }
    return dict(sorted((key, value) for key, value in result.items() if value))


def _positions(values: Mapping[str, Any], name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_instrument, raw_quantity in values.items():
        instrument = str(raw_instrument)
        if not instrument or isinstance(raw_quantity, bool):
            raise ValueError(f"invalid {name}")
        quantity = int(raw_quantity)
        if quantity != raw_quantity:
            raise ValueError(f"{name} for {instrument} must be an integer")
        if quantity:
            result[instrument] = quantity
    return dict(sorted(result.items()))


def _add(result: dict[str, int], instrument: str, quantity: int, name: str) -> None:
    if not instrument or isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError(f"invalid {name}")
    if quantity == 0:
        raise ValueError(f"{name} quantity must not be zero")
    result[instrument] = result.get(instrument, 0) + quantity
    if result[instrument] == 0:
        result.pop(instrument)
