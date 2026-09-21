"""Build a live risk snapshot and desired Beta target without placing orders."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    build_hedge_proposal,
    Greeks,
    InstrumentGreeks,
    StrategyLedger,
    evaluate_delta_gamma_hedge,
    integerize_delta_gamma_hedge,
)
from sim_hedge.domain import OptionType
from sim_hedge.pricing_types import PricingBatch
from sim_hedge.strategy_universe import StrategyUniverse


def build_live_risk_snapshot(
    pricing: PricingBatch,
    universe: StrategyUniverse,
    ledger: StrategyLedger,
    *,
    spot: float,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    """Return risk and a desired Beta position from one pricing result."""

    if spot <= 0:
        raise ValueError("spot must be positive")
    overlap = set(ledger.alpha_positions) & set(ledger.beta_positions)
    if overlap:
        raise ValueError(
            "Alpha and Beta positions overlap: " + ", ".join(sorted(overlap))
        )

    contracts = {contract.instrument: contract for contract in universe.contracts}
    held = set(ledger.alpha_positions) | set(ledger.beta_positions)
    outside = held - set(contracts)
    if outside:
        raise ValueError(
            "held positions are outside the live strategy universe: "
            + ", ".join(sorted(outside))
        )

    results = {result.instrument: result for result in pricing.results}
    greeks: dict[str, InstrumentGreeks] = {}
    exclusions: dict[str, str] = {}
    for instrument, contract in contracts.items():
        result = results.get(instrument)
        if (
            result is None
            or result.status != "OK"
            or result.delta is None
            or result.gamma is None
            or result.vega_per_absolute_volatility is None
            or result.theta_per_year is None
        ):
            exclusions[instrument] = (
                "missing pricing result"
                if result is None
                else result.error or f"pricing status is {result.status}"
            )
            continue
        multiplier = contract.contract_multiplier
        greeks[instrument] = InstrumentGreeks(
            instrument,
            Greeks(
                delta=result.delta * multiplier,
                gamma=result.gamma * multiplier,
                vega=result.vega_per_absolute_volatility * multiplier,
                theta=result.theta_per_year * multiplier,
            ),
        )

    invalid_held = held & set(exclusions)
    if invalid_held:
        raise ValueError(
            "valid pricing Greeks missing for " + ", ".join(sorted(invalid_held))
        )

    hedge_pair = _select_hedge_pair(
        universe,
        spot=spot,
        allowed=set(greeks) - set(ledger.alpha_positions),
        greeks=greeks,
    )
    decision = evaluate_delta_gamma_hedge(
        alpha_positions=ledger.alpha_positions,
        hedge_positions=ledger.beta_positions,
        instrument_greeks=greeks,
        hedge_pair=hedge_pair,
    )
    tradable = integerize_delta_gamma_hedge(
        decision,
        instrument_greeks=greeks,
        hedge_pair=hedge_pair,
    )
    timestamp = published_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    proposal = build_hedge_proposal(
        pricing_request_id=pricing.request_id,
        account_id=ledger.account_id,
        base_ledger_revision=ledger.revision,
        created_at=pricing.calculated_at,
        engine_name="simple-delta-gamma-pair",
        engine_version="1",
        confirmed_beta_positions=ledger.beta_positions,
        incremental_trades=tradable.integer_incremental_trades,
    )
    return {
        **proposal,
        "status": "READY",
        "pricing_calculated_at": pricing.calculated_at.isoformat(),
        "published_at": timestamp.astimezone(timezone.utc).isoformat(),
        "spot": spot,
        "hedge_pair": list(hedge_pair),
        "actual_alpha_positions": dict(ledger.alpha_positions),
        "risk": {
            "alpha": asdict(decision.alpha_risk),
            "beta": asdict(decision.current_hedge_risk),
            "portfolio": asdict(decision.before_hedge),
            "at_desired_beta": asdict(tradable.after_integer_hedge),
        },
        "continuous_incremental_trades": dict(decision.incremental_trades),
        "normalized_residual": tradable.normalized_residual,
        "pricing_exclusions": exclusions,
    }


def write_live_risk_snapshot(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish the latest state for another process to consume."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _select_hedge_pair(
    universe: StrategyUniverse,
    *,
    spot: float,
    allowed: set[str],
    greeks: Mapping[str, InstrumentGreeks],
) -> tuple[str, str]:
    calls = sorted(
        (
            contract
            for contract in universe.contracts
            if contract.instrument in allowed and contract.option_type is OptionType.CALL
        ),
        key=lambda contract: abs(contract.strike - spot),
    )
    puts = sorted(
        (
            contract
            for contract in universe.contracts
            if contract.instrument in allowed and contract.option_type is OptionType.PUT
        ),
        key=lambda contract: abs(contract.strike - spot),
    )
    pairs = sorted(
        ((call, put) for call in calls for put in puts),
        key=lambda pair: abs(pair[0].strike - spot) + abs(pair[1].strike - spot),
    )
    for call, put in pairs:
        first = greeks[call.instrument].greeks_per_contract
        second = greeks[put.instrument].greeks_per_contract
        determinant = first.delta * second.gamma - second.delta * first.gamma
        if abs(determinant) >= 1e-12:
            return call.instrument, put.instrument
    raise ValueError("no non-singular call/put hedge pair outside the Alpha legs")
