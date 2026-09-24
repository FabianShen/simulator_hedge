"""Build a vendor-neutral hedge request from one priced market snapshot."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from hedge_engine import StrategyLedger
from sim_hedge.pricing_types import PricingBatch


def build_hedge_request(
    pricing_request: Mapping[str, Any],
    pricing_result: PricingBatch,
    ledger: StrategyLedger,
    *,
    hedge_candidates: Iterable[str],
) -> dict[str, Any]:
    request_id = str(pricing_request.get("requestId") or "")
    if not request_id or pricing_result.request_id != request_id:
        raise ValueError("pricing request and result IDs do not match")
    underlying = _mapping(pricing_request.get("underlying"), "underlying")
    spot = float(underlying.get("spot") or 0)
    if spot <= 0:
        raise ValueError("underlying spot must be positive")
    as_of = str(pricing_request.get("asOf") or "")
    if not as_of:
        raise ValueError("pricing request asOf is missing")

    options = pricing_request.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("pricing options must be a non-empty list")
    metadata = {
        str(_mapping(item, "pricing option").get("instrument") or ""): item
        for item in options
    }
    results = {result.instrument: result for result in pricing_result.results}
    held = set(ledger.alpha_positions) | set(ledger.beta_positions)
    instruments = []
    invalid: dict[str, str] = {}
    for instrument, raw_option in metadata.items():
        option = _mapping(raw_option, f"pricing option {instrument}")
        result = results.get(instrument)
        if (
            result is None
            or result.status != "OK"
            or result.delta is None
            or result.gamma is None
            or result.theta_per_year is None
            or result.vega_per_absolute_volatility is None
        ):
            invalid[instrument] = (
                "missing pricing result"
                if result is None
                else result.error or f"pricing status is {result.status}"
            )
            continue
        instruments.append(
            {
                "instrument": instrument,
                "optionType": str(option.get("optionType") or ""),
                "strike": option.get("strike"),
                "contractMultiplier": option.get("contractMultiplier"),
                "delta": result.delta,
                "gamma": result.gamma,
                "thetaPerYear": result.theta_per_year,
                "vegaPerAbsoluteVolatility": result.vega_per_absolute_volatility,
            }
        )
    invalid_held = held & set(invalid)
    if invalid_held:
        details = "; ".join(
            f"{instrument}: {invalid[instrument]}"
            for instrument in sorted(invalid_held)
        )
        raise ValueError(f"valid pricing Greeks missing for held positions: {details}")
    missing_held = held - set(metadata)
    if missing_held:
        raise ValueError(
            "held positions are outside the pricing request: "
            + ", ".join(sorted(missing_held))
        )

    eligible = set(hedge_candidates)
    if eligible - set(metadata):
        raise ValueError("hedge candidates are outside the pricing request")
    hedge_universe = [
        item["instrument"]
        for item in instruments
        if item["instrument"] in eligible
    ]
    return {
        "requestId": f"hedge-{request_id}-ledger-{ledger.revision}",
        "sourcePricingRequestId": request_id,
        "marketAsOf": as_of,
        "accountId": ledger.account_id,
        "baseStrategyLedgerRevision": ledger.revision,
        "spot": spot,
        "instruments": instruments,
        "confirmedAlphaPositions": dict(ledger.alpha_positions),
        "confirmedBetaPositions": dict(ledger.beta_positions),
        "hedgeUniverse": hedge_universe,
        "configuration": {"model": "HEDGE_MODEL_SIMPLE_DELTA_GAMMA_PAIR"},
    }


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value
