"""Versioned, execution-neutral hedge proposal contract."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping


HEDGE_PROPOSAL_VERSION = "sim-hedge/hedge-proposal/v1"


def build_hedge_proposal(
    *,
    pricing_request_id: str,
    account_id: str,
    base_ledger_revision: int,
    created_at: datetime,
    engine_name: str,
    engine_version: str,
    confirmed_beta_positions: Mapping[str, int],
    incremental_trades: Mapping[str, int],
) -> dict[str, Any]:
    """Describe one incremental decision against one confirmed ledger state."""

    if not pricing_request_id:
        raise ValueError("pricing_request_id must not be empty")
    if not account_id:
        raise ValueError("account_id must not be empty")
    if base_ledger_revision < 0:
        raise ValueError("base_ledger_revision must not be negative")
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    if not engine_name or not engine_version:
        raise ValueError("engine name and version must not be empty")
    confirmed = _positions(confirmed_beta_positions, "confirmed Beta position")
    incremental = _positions(incremental_trades, "incremental trade")
    target = dict(confirmed)
    for instrument, quantity in incremental.items():
        updated = target.get(instrument, 0) + quantity
        if updated:
            target[instrument] = updated
        else:
            target.pop(instrument, None)
    identity = {
        "protocol_version": HEDGE_PROPOSAL_VERSION,
        "pricing_request_id": pricing_request_id,
        "account_id": account_id,
        "base_strategy_ledger_revision": base_ledger_revision,
        "decision_engine": {"name": engine_name, "version": engine_version},
        "confirmed_beta_positions": confirmed,
        "incremental_trades": incremental,
        "target_beta_positions": target,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    proposal_id = f"hedge-{sha256(canonical.encode('utf-8')).hexdigest()[:20]}"
    return {
        "protocol_version": HEDGE_PROPOSAL_VERSION,
        "proposal_id": proposal_id,
        "proposal_type": "INCREMENTAL_BETA",
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "source_pricing_request_id": pricing_request_id,
        "account_id": account_id,
        "base_strategy_ledger_revision": base_ledger_revision,
        "decision_engine": {"name": engine_name, "version": engine_version},
        "confirmed_beta_positions": confirmed,
        "incremental_trades": incremental,
        "target_beta_positions": target,
        "orders_generated": False,
    }


def validate_hedge_proposal(
    proposal: Mapping[str, Any],
    *,
    pricing_request_id: str,
    account_id: str,
    base_ledger_revision: int,
    confirmed_beta_positions: Mapping[str, int],
) -> dict[str, int]:
    """Validate proposal identity and return its integer incremental trades."""

    if proposal.get("protocol_version") != HEDGE_PROPOSAL_VERSION:
        raise ValueError("unsupported hedge proposal protocol version")
    if proposal.get("proposal_type") != "INCREMENTAL_BETA":
        raise ValueError("hedge proposal type must be INCREMENTAL_BETA")
    if proposal.get("source_pricing_request_id") != pricing_request_id:
        raise ValueError("hedge proposal and pricing request IDs do not match")
    if proposal.get("account_id") != account_id:
        raise ValueError("hedge proposal and strategy ledger accounts do not match")
    if proposal.get("base_strategy_ledger_revision") != base_ledger_revision:
        raise ValueError("hedge proposal and strategy ledger revisions do not match")
    if proposal.get("orders_generated") is not False:
        raise ValueError("hedge proposal must not generate orders")
    engine = _mapping(proposal.get("decision_engine"), "decision engine")
    engine_name = str(engine.get("name") or "")
    engine_version = str(engine.get("version") or "")
    if not engine_name or not engine_version:
        raise ValueError("decision engine name and version must not be empty")

    confirmed = _positions(
        _mapping(proposal.get("confirmed_beta_positions"), "confirmed Beta positions"),
        "confirmed Beta position",
    )
    expected_confirmed = _positions(
        confirmed_beta_positions, "ledger confirmed Beta position"
    )
    if confirmed != expected_confirmed:
        raise ValueError(
            "hedge proposal confirmed Beta does not match the strategy ledger"
        )
    incremental = _positions(
        _mapping(proposal.get("incremental_trades"), "incremental trades"),
        "incremental trade",
    )
    target = _positions(
        _mapping(proposal.get("target_beta_positions"), "target Beta positions"),
        "target Beta position",
    )
    expected = dict(confirmed)
    for instrument, quantity in incremental.items():
        updated = expected.get(instrument, 0) + quantity
        if updated:
            expected[instrument] = updated
        else:
            expected.pop(instrument, None)
    if target != expected:
        raise ValueError(
            "target Beta positions must equal confirmed Beta plus incremental trades"
        )

    identity = {
        "protocol_version": HEDGE_PROPOSAL_VERSION,
        "pricing_request_id": pricing_request_id,
        "account_id": account_id,
        "base_strategy_ledger_revision": base_ledger_revision,
        "decision_engine": {"name": engine_name, "version": engine_version},
        "confirmed_beta_positions": confirmed,
        "incremental_trades": incremental,
        "target_beta_positions": target,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    expected_id = f"hedge-{sha256(canonical.encode('utf-8')).hexdigest()[:20]}"
    if proposal.get("proposal_id") != expected_id:
        raise ValueError("hedge proposal ID does not match its contents")
    return incremental


def _positions(values: Mapping[str, Any], name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_instrument, raw_quantity in values.items():
        instrument = str(raw_instrument)
        if not instrument:
            raise ValueError(f"{name} instrument must not be empty")
        if isinstance(raw_quantity, bool):
            raise ValueError(f"{name} for {instrument} must be an integer")
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} for {instrument} must be an integer") from exc
        if quantity != raw_quantity:
            raise ValueError(f"{name} for {instrument} must be an integer")
        if quantity:
            result[instrument] = quantity
    return dict(sorted(result.items()))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value
