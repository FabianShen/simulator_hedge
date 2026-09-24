"""Translate read-only broker facts into the pure execution-state model."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from execution_engine import (
    ConfirmedExecutionFill,
    ExecutionAssessment,
    WorkingExecutionOrder,
    assess_execution,
    start_execution_batch,
)
from hedge_engine import (
    OrderIntent,
    OrderRegistry,
    StrategyLedger,
    combined_strategy_positions,
)
from sim_hedge.domain.portfolio import ActiveOrderSnapshot, PortfolioSnapshot


def assess_broker_execution(
    proposal: Mapping[str, Any],
    *,
    ledger: StrategyLedger,
    registry: OrderRegistry,
    portfolio: PortfolioSnapshot,
) -> ExecutionAssessment:
    """Assess one proposal using saved ownership and read-only broker state."""

    account_id = str(proposal.get("account_id") or "")
    if not account_id or {
        ledger.account_id,
        registry.account_id,
        portfolio.account.account_id,
    } != {account_id}:
        raise ValueError("proposal, ledger, registry, and portfolio accounts differ")
    if registry.unknown_client_order_ids:
        raise ValueError("order registry contains unknown submission outcomes")
    if portfolio.signed_positions != combined_strategy_positions(ledger):
        raise ValueError(
            "broker positions and strategy ledger differ; reconcile before execution"
        )

    base_revision = _integer(
        proposal.get("base_strategy_ledger_revision"), "base ledger revision"
    )
    if ledger.revision < base_revision:
        raise ValueError("strategy ledger is older than the hedge proposal")
    base_beta = _positions(
        _mapping(proposal.get("confirmed_beta_positions"), "confirmed Beta"),
        "confirmed Beta",
    )
    batch = start_execution_batch(
        proposal,
        pricing_request_id=str(proposal.get("source_pricing_request_id") or ""),
        account_id=account_id,
        ledger_revision=base_revision,
        confirmed_beta_positions=base_beta,
    )

    fills: list[ConfirmedExecutionFill] = []
    for fill in ledger.applied_trades.values():
        if fill.strategy != "BETA":
            continue
        intent = _intent_for_order(registry, fill.order_id)
        if intent is not None and intent.proposal_id == batch.proposal_id:
            fills.append(
                ConfirmedExecutionFill(
                    fill_id=fill.trade_id,
                    proposal_id=batch.proposal_id,
                    instrument=fill.instrument,
                    quantity=fill.quantity,
                )
            )

    expected_beta = dict(base_beta)
    for fill in fills:
        _apply(expected_beta, fill.instrument, fill.quantity)
    if dict(ledger.beta_positions) != dict(sorted(expected_beta.items())):
        raise ValueError(
            "confirmed Beta changed outside this proposal; assess a newer proposal"
        )

    confirmed_by_order: dict[str, int] = {}
    for fill in ledger.applied_trades.values():
        confirmed_by_order[fill.order_id] = (
            confirmed_by_order.get(fill.order_id, 0) + fill.quantity
        )
    working = tuple(
        _working_order(order, registry, confirmed_by_order)
        for order in portfolio.active_orders
    )
    return assess_execution(
        batch,
        confirmed_fills=tuple(fills),
        working_orders=working,
    )


def _working_order(
    order: ActiveOrderSnapshot,
    registry: OrderRegistry,
    confirmed_by_order: Mapping[str, int],
) -> WorkingExecutionOrder:
    intent = _intent_for_order(registry, order.order_id)
    if intent is None:
        raise ValueError(f"active broker order {order.order_id} is not registered")
    if intent.strategy != "BETA" or intent.proposal_id is None:
        raise ValueError(
            f"active broker order {order.order_id} has no Beta proposal ownership"
        )
    if order.account_id != intent.account_id or order.instrument != intent.instrument:
        raise ValueError(f"active broker order {order.order_id} conflicts with intent")
    expected_direction = "BUY" if intent.quantity > 0 else "SELL"
    if order.direction != expected_direction:
        raise ValueError(f"active broker order {order.order_id} has wrong direction")
    total = _whole(order.total_volume, "active order total volume")
    traded = _whole(order.traded_volume, "active order traded volume")
    remaining = _whole(order.remaining_volume, "active order remaining volume")
    if total != abs(intent.quantity) or traded + remaining != total:
        raise ValueError(f"active broker order {order.order_id} conflicts with intent")
    confirmed = confirmed_by_order.get(order.order_id, 0)
    expected_confirmed = traded if intent.quantity > 0 else -traded
    if confirmed != expected_confirmed:
        raise ValueError(
            f"active broker order {order.order_id} has unreconciled fills"
        )
    if remaining <= 0:
        raise ValueError(f"active broker order {order.order_id} has invalid remainder")
    return WorkingExecutionOrder(
        client_order_id=intent.client_order_id,
        proposal_id=intent.proposal_id,
        instrument=intent.instrument,
        remaining_quantity=remaining if intent.quantity > 0 else -remaining,
    )


def _intent_for_order(
    registry: OrderRegistry, order_id: str
) -> OrderIntent | None:
    client_id = registry.broker_orders.get(order_id)
    return None if client_id is None else registry.intents[client_id]


def _apply(positions: dict[str, int], instrument: str, quantity: int) -> None:
    updated = positions.get(instrument, 0) + quantity
    if updated:
        positions[instrument] = updated
    else:
        positions.pop(instrument, None)


def _positions(values: Mapping[str, Any], name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_instrument, raw_quantity in values.items():
        instrument = str(raw_instrument)
        quantity = _integer(raw_quantity, f"{name} {instrument}")
        if not instrument:
            raise ValueError(f"{name} instrument must not be empty")
        if quantity:
            result[instrument] = quantity
    return dict(sorted(result.items()))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
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


def _whole(value: Decimal, name: str) -> int:
    if value != value.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(value)
