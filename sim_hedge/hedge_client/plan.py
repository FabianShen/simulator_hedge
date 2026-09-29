"""Replay pricing and write an offline Delta/Gamma hedge decision."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    Greeks,
    InstrumentGreeks,
    MarketSnapshot,
    build_hedge_context,
)
from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine
from pricing_engine import SabrPricingEngine
from pricing_engine.__main__ import load_request
from sim_hedge.jsonio import (
    coerce_int as _integer_quantity,
    read_json as _read,
    require_object as _object,
    write_json as _write,
)
from hedge_engine.config import HedgeConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an offline hedge decision")
    parser.add_argument("pricing_request")
    parser.add_argument("portfolio_snapshot")
    parser.add_argument("strategy_ledger")
    parser.add_argument("--output", default="outputs/hedge_plan.json")
    parser.add_argument("--max-market-age", type=float, default=10.0)
    parser.add_argument("--capital", type=float, default=100_000_000.0)
    parser.add_argument(
        "--delta-entry-risk-band", type=float, default=30_000.0,
    )
    parser.add_argument("--delta-target-risk-band", type=float, default=10_000.0)
    parser.add_argument(
        "--gamma-entry-risk-band", type=float, default=10_000.0,
    )
    parser.add_argument("--gamma-target-risk-band", type=float, default=10_000.0)
    parser.add_argument(
        "--target-delta", type=float, default=0.0,
        help="raw-Greek Delta center; a nonzero value requires --delta-limit",
    )
    parser.add_argument(
        "--target-gamma", type=float, default=0.0,
        help="raw-Greek Gamma center; a nonzero value requires --gamma-limit",
    )
    parser.add_argument(
        "--delta-limit", type=float, default=None,
        help="raw-Greek Delta tolerance around --target-delta",
    )
    parser.add_argument(
        "--gamma-limit", type=float, default=None,
        help="raw-Greek Gamma tolerance around --target-gamma",
    )
    args = parser.parse_args()
    try:
        pricing_payload = _object(_read(args.pricing_request), "pricing request")
        portfolio_payload = _object(_read(args.portfolio_snapshot), "portfolio")
        ledger_payload = _object(_read(args.strategy_ledger), "strategy ledger")
        result, context, pricing_exclusions = build_offline_hedge_decision(
            Path(args.pricing_request),
            pricing_payload,
            portfolio_payload,
            ledger_payload,
            max_market_age_seconds=args.max_market_age,
            capital=args.capital,
            config=HedgeConfig(
                delta_entry_risk_band=args.delta_entry_risk_band,
                delta_target_risk_band=args.delta_target_risk_band,
                gamma_entry_risk_band=args.gamma_entry_risk_band,
                gamma_target_risk_band=args.gamma_target_risk_band,
                target_delta=args.target_delta,
                target_gamma=args.target_gamma,
                delta_limit=args.delta_limit,
                gamma_limit=args.gamma_limit,
            ),
        )
        output = {
            **result.proposal,
            "source_market_as_of": pricing_payload.get("asOf"),
            "hedge_pair": list(result.hedge_pair),
            "strategy_universe": list(context.strategy_universe),
            "hedge_universe": list(context.hedge_universe),
            "pricing_exclusions": pricing_exclusions,
            "risk": {
                "alpha": asdict(result.alpha_risk),
                "current_beta": asdict(result.confirmed_beta_risk),
                "before_hedge": asdict(result.portfolio_risk),
            },
            "risk_at_target_beta": asdict(result.risk_at_target_beta),
            "gamma_improvement": result.gamma_improvement,
            "decision_policy": result.decision_policy,
            "execution_diagnostics": asdict(result.execution_diagnostics),
        }
        _write(args.output, output)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"hedge plan failed: {exc}") from exc
    print(
        f"active hedge legs: {', '.join(result.hedge_pair) or 'none'} "
        f"before delta={result.portfolio_risk.delta:.6f} "
        f"gamma={result.portfolio_risk.gamma:.6f}"
    )
    print(
        f"after delta={result.risk_at_target_beta.delta:.6g} "
        f"gamma={result.risk_at_target_beta.gamma:.6g}"
    )
    print(
        "integer Beta trades: "
        + ", ".join(
            f"{instrument}={quantity:+d}"
            for instrument, quantity in result.proposal["incremental_trades"].items()
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
    config: HedgeConfig | None = None,
    capital: float = 100_000_000.0,
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
    quoted_metadata = {
        instrument: option
        for instrument, option in metadata.items()
        if _is_quoted_option(option, instrument)
    }
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
            for code, option in quoted_metadata.items()
            if code in instrument_greeks
        },
        observed_at={
            code: _datetime(str(option["observedAt"]))
            for code, option in quoted_metadata.items()
            if code in instrument_greeks
        },
    )
    context = build_hedge_context(
        market=market,
        alpha_positions=alpha_positions,
        beta_positions=beta_positions,
        broker_positions=broker_positions,
        strategy_universe=tuple(
            instrument for instrument in quoted_metadata
            if instrument in instrument_greeks
        ),
        instrument_greeks=instrument_greeks,
        max_market_age_seconds=max_market_age_seconds,
    )
    pricing_request_id = str(pricing_payload.get("requestId") or "")
    ledger_revision = _integer_quantity(ledger_payload.get("revision"), "ledger revision")
    request = HedgeRequest(
        request_id=f"hedge-{pricing_request_id}-ledger-{ledger_revision}",
        source_pricing_request_id=pricing_request_id,
        market_as_of=market.as_of,
        account_id=ledger_account,
        base_ledger_revision=ledger_revision,
        spot=market.spot,
        instruments=tuple(
            HedgeInstrument(
                instrument=instrument,
                option_type=str(option["optionType"]).removeprefix("OPTION_TYPE_"),
                strike=float(option["strike"]),
                contract_multiplier=_integer_quantity(
                    option["contractMultiplier"], f"contract multiplier for {instrument}"
                ),
                delta=float(results[instrument].delta),
                gamma=float(results[instrument].gamma),
                theta=float(results[instrument].theta_per_year),
                vega=float(results[instrument].vega_per_absolute_volatility),
                bid=(float(option["bid"]) if option.get("bid") is not None else None),
                ask=(float(option["ask"]) if option.get("ask") is not None else None),
                bid_size=(
                    _integer_quantity(option["bidSize"], f"bid size for {instrument}")
                    if option.get("bidSize") is not None else None
                ),
                ask_size=(
                    _integer_quantity(option["askSize"], f"ask size for {instrument}")
                    if option.get("askSize") is not None else None
                ),
            )
            for instrument, option in valid_metadata.items()
        ),
        confirmed_alpha_positions=context.alpha_positions,
        confirmed_beta_positions=context.beta_positions,
        hedge_universe=context.hedge_universe,
    )
    result = ReferenceHedgeEngine(config=config, capital=capital).propose(
        request, created_at=datetime.now(timezone.utc)
    )
    return result, context, pricing_exclusions


def _is_quoted_option(option: Mapping[str, Any], instrument: str) -> bool:
    has_price = "marketPrice" in option
    has_observed_at = "observedAt" in option
    if has_price != has_observed_at:
        raise ValueError(
            f"quoted option {instrument} must contain both marketPrice and observedAt"
        )
    return has_price


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
        volume = _decimal_quantity(position["volume"], f"broker position {code}")
        direction = str(position.get("direction") or "").upper()
        if direction in {"SHORT", "SELL"}:
            signed = -volume
        elif direction in {"LONG", "BUY"}:
            signed = volume
        else:
            raise ValueError(f"unknown broker position direction for {code}: {direction!r}")
        result[code] = result.get(code, 0) + signed
    return {code: quantity for code, quantity in result.items() if quantity}


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal_quantity(value: Any, name: str) -> int:
    """Read an integral Decimal serialized by portfolio_remote as JSON text."""

    quantity = Decimal(str(value))
    if not quantity.is_finite() or quantity != quantity.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(quantity)


if __name__ == "__main__":
    main()
