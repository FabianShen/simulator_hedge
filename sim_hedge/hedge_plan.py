"""Replay pricing and write an offline Delta/Gamma hedge decision."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    Greeks,
    InstrumentGreeks,
    MarketSnapshot,
    build_hedge_context,
    evaluate_delta_gamma_hedge,
    integerize_delta_gamma_hedge,
)
from pricing_engine import SabrPricingEngine
from pricing_engine.__main__ import load_request


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an offline hedge decision")
    parser.add_argument("pricing_request")
    parser.add_argument("portfolio_snapshot")
    parser.add_argument("strategy_ledger")
    parser.add_argument("--output", default="outputs/hedge_plan.json")
    parser.add_argument("--max-market-age", type=float, default=10.0)
    args = parser.parse_args()
    try:
        pricing_payload = _object(_read(args.pricing_request), "pricing request")
        portfolio_payload = _object(_read(args.portfolio_snapshot), "portfolio")
        ledger_payload = _object(_read(args.strategy_ledger), "strategy ledger")
        (
            decision,
            hedge_pair,
            instrument_greeks,
            context,
            pricing_exclusions,
        ) = build_offline_hedge_decision(
            Path(args.pricing_request),
            pricing_payload,
            portfolio_payload,
            ledger_payload,
            max_market_age_seconds=args.max_market_age,
        )
        tradable = integerize_delta_gamma_hedge(
            decision,
            instrument_greeks=instrument_greeks,
            hedge_pair=hedge_pair,
        )
        output = {
            "source_pricing_request_id": pricing_payload.get("requestId"),
            "source_strategy_ledger_revision": ledger_payload.get("revision"),
            "hedge_pair": list(hedge_pair),
            "strategy_universe": list(context.strategy_universe),
            "hedge_universe": list(context.hedge_universe),
            "pricing_exclusions": pricing_exclusions,
            "orders_generated": False,
            "risk": {
                "alpha": asdict(decision.alpha_risk),
                "current_beta": asdict(decision.current_hedge_risk),
                "before_hedge": asdict(decision.before_hedge),
            },
            "continuous_solution": {
                "quantity_type": "CONTINUOUS_INCREMENTAL_TRADE",
                "incremental_trades": dict(decision.incremental_trades),
                "after_hedge": asdict(decision.after_hedge),
            },
            "tradable_solution": {
                "quantity_type": "INTEGER_INCREMENTAL_TRADE",
                "incremental_trades": dict(tradable.integer_incremental_trades),
                "after_hedge": asdict(tradable.after_integer_hedge),
                "normalized_residual": tradable.normalized_residual,
            },
        }
        _write(args.output, output)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"hedge plan failed: {exc}") from exc
    print(
        f"hedge pair: {hedge_pair[0]}, {hedge_pair[1]} "
        f"before delta={decision.before_hedge.delta:.6f} "
        f"gamma={decision.before_hedge.gamma:.6f}"
    )
    print(
        f"after delta={decision.after_hedge.delta:.6g} "
        f"gamma={decision.after_hedge.gamma:.6g}"
    )
    print(
        "integer Beta trades: "
        + ", ".join(
            f"{instrument}={quantity:+d}"
            for instrument, quantity in tradable.integer_incremental_trades.items()
        )
    )
    if pricing_exclusions:
        print(
            "excluded unheld contracts: "
            + ", ".join(sorted(pricing_exclusions))
        )
    print(f"wrote offline hedge decision: {args.output}")


def build_offline_hedge_decision(
    pricing_path: Path,
    pricing_payload: Mapping[str, Any],
    portfolio_payload: Mapping[str, Any],
    ledger_payload: Mapping[str, Any],
    *,
    max_market_age_seconds: float = 10.0,
):
    if ledger_payload.get("status") != "CONFIRMED":
        raise ValueError("strategy ledger must contain confirmed fills")
    account = _object(portfolio_payload.get("account"), "portfolio account")
    ledger_account = str(ledger_payload.get("account_id") or "")
    if not ledger_account or ledger_account != str(account.get("account_id") or ""):
        raise ValueError("strategy ledger and portfolio account IDs do not match")
    active_orders = portfolio_payload.get("active_orders")
    if not isinstance(active_orders, list):
        raise ValueError("portfolio active_orders must be a list")
    if active_orders:
        raise ValueError("cannot plan a hedge while broker orders are active")
    options = pricing_payload.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("pricing options must be a non-empty list")

    strategies = _object(ledger_payload.get("strategies"), "ledger strategies")
    alpha_positions = _actual_positions(strategies, "ALPHA")
    beta_positions = _actual_positions(strategies, "BETA")
    held_instruments = set(alpha_positions) | set(beta_positions)
    broker_positions = _broker_positions(portfolio_payload)

    response = SabrPricingEngine().price(load_request(pricing_path))
    results = {result.instrument: result for result in response.results}
    nearest_expiry = min(_datetime(str(option["expiry"])) for option in options)
    selected_options = [
        option
        for option in options
        if _datetime(str(option["expiry"])) == nearest_expiry
    ]
    metadata = {str(option["instrument"]): option for option in selected_options}
    outside_nearest = held_instruments - set(metadata)
    if outside_nearest:
        raise ValueError(
            "held positions are outside the nearest-expiry pricing universe: "
            + ", ".join(sorted(outside_nearest))
        )
    instrument_greeks, pricing_exclusions = _usable_instrument_greeks(
        metadata, results, held_instruments
    )
    valid_metadata = {
        instrument: option
        for instrument, option in metadata.items()
        if instrument in instrument_greeks
    }
    underlying = _object(pricing_payload.get("underlying"), "underlying")
    market = MarketSnapshot(
        as_of=_datetime(str(pricing_payload["asOf"])),
        spot=float(underlying["spot"]),
        spot_observed_at=_datetime(str(underlying["observedAt"])),
        marks={
            code: float(option["marketPrice"])
            for code, option in valid_metadata.items()
        },
        observed_at={
            code: _datetime(str(option["observedAt"]))
            for code, option in valid_metadata.items()
        },
    )
    context = build_hedge_context(
        market=market,
        alpha_positions=alpha_positions,
        beta_positions=beta_positions,
        broker_positions=broker_positions,
        strategy_universe=tuple(valid_metadata),
        instrument_greeks=instrument_greeks,
        max_market_age_seconds=max_market_age_seconds,
    )
    hedge_pair = _select_hedge_pair(
        pricing_payload, set(context.hedge_universe), instrument_greeks
    )
    decision = evaluate_delta_gamma_hedge(
        alpha_positions=context.alpha_positions,
        hedge_positions=context.beta_positions,
        instrument_greeks=instrument_greeks,
        hedge_pair=hedge_pair,
    )
    return decision, hedge_pair, instrument_greeks, context, pricing_exclusions


def _usable_instrument_greeks(
    metadata: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Any],
    held_instruments: set[str],
) -> tuple[dict[str, InstrumentGreeks], dict[str, dict[str, str]]]:
    instrument_greeks: dict[str, InstrumentGreeks] = {}
    exclusions: dict[str, dict[str, str]] = {}
    for instrument, option in metadata.items():
        result = results.get(instrument)
        invalid = (
            result is None
            or result.status != "OK"
            or result.delta is None
            or result.gamma is None
            or result.vega_per_absolute_volatility is None
            or result.theta_per_year is None
        )
        if invalid:
            exclusions[instrument] = _pricing_exclusion(result)
            continue
        multiplier = float(option["contractMultiplier"])
        instrument_greeks[instrument] = InstrumentGreeks(
            instrument,
            Greeks(
                delta=result.delta * multiplier,
                gamma=result.gamma * multiplier,
                vega=result.vega_per_absolute_volatility * multiplier,
                theta=result.theta_per_year * multiplier,
            ),
        )
    invalid_held = held_instruments & set(exclusions)
    if invalid_held:
        details = "; ".join(
            f"{instrument}: {exclusions[instrument]['reason']}"
            for instrument in sorted(invalid_held)
        )
        raise ValueError(f"valid pricing Greeks missing for held positions: {details}")
    return instrument_greeks, exclusions


def _pricing_exclusion(result: Any) -> dict[str, str]:
    if result is None:
        return {"status": "MISSING", "reason": "pricing result is missing"}
    missing = [
        name
        for name, value in (
            ("delta", result.delta),
            ("gamma", result.gamma),
            ("vega", result.vega_per_absolute_volatility),
            ("theta", result.theta_per_year),
        )
        if value is None
    ]
    reason = result.error or (
        "missing Greeks: " + ", ".join(missing)
        if missing
        else f"pricing status is {result.status}"
    )
    return {"status": str(result.status), "reason": reason}


def _select_hedge_pair(
    pricing: Mapping[str, Any],
    allowed: set[str],
    greeks: Mapping[str, InstrumentGreeks],
) -> tuple[str, str]:
    spot = float(_object(pricing.get("underlying"), "underlying")["spot"])
    candidates = [
        option for option in pricing["options"] if option["instrument"] in allowed
    ]
    calls = sorted(
        (option for option in candidates if option["optionType"] == "OPTION_TYPE_CALL"),
        key=lambda option: abs(float(option["strike"]) - spot),
    )
    puts = sorted(
        (option for option in candidates if option["optionType"] == "OPTION_TYPE_PUT"),
        key=lambda option: abs(float(option["strike"]) - spot),
    )
    pairs = sorted(
        ((call, put) for call in calls for put in puts),
        key=lambda pair: abs(float(pair[0]["strike"]) - spot)
        + abs(float(pair[1]["strike"]) - spot),
    )
    for call, put in pairs:
        first = greeks[str(call["instrument"])].greeks_per_contract
        second = greeks[str(put["instrument"])].greeks_per_contract
        determinant = first.delta * second.gamma - second.delta * first.gamma
        if abs(determinant) >= 1e-12:
            return str(call["instrument"]), str(put["instrument"])
    raise ValueError("no non-singular call/put hedge pair outside the Alpha legs")


def _actual_positions(
    strategies: Mapping[str, Any], strategy: str
) -> dict[str, int]:
    book = _object(strategies.get(strategy), f"{strategy} strategy")
    positions = _object(book.get("actual_positions"), f"{strategy} actual positions")
    return {
        str(code): _integer_quantity(quantity, f"{strategy} position {code}")
        for code, quantity in positions.items()
    }


def _broker_positions(portfolio: Mapping[str, Any]) -> dict[str, int]:
    raw_positions = portfolio.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("portfolio positions must be a list")
    result: dict[str, int] = {}
    for raw in raw_positions:
        position = _object(raw, "broker position")
        code = str(position.get("instrument") or "")
        if not code:
            raise ValueError("broker position is missing instrument")
        volume = _integer_quantity(position["volume"], f"broker position {code}")
        direction = str(position.get("direction") or "").upper()
        if direction in {"SHORT", "SELL"}:
            signed = -volume
        elif direction in {"LONG", "BUY"}:
            signed = volume
        else:
            raise ValueError(f"unknown broker position direction for {code}: {direction!r}")
        result[code] = result.get(code, 0) + signed
    return {code: quantity for code, quantity in result.items() if quantity}


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} is not an object")
    return value


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _integer_quantity(value: Any, name: str) -> int:
    quantity = Decimal(str(value))
    if quantity != quantity.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(quantity)


if __name__ == "__main__":
    main()
