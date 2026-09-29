"""Thin adapter from a versioned hedge request to policy state."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from hedge_engine.engine import Greeks, InstrumentGreeks, aggregate_greeks
from .actions import scenario_risk
from .config import HedgeConfig
from .margin import short_option_margin


@dataclass(frozen=True)
class HedgeContext:
    strategy_positions: Mapping[str, int]
    hedge_positions: Mapping[str, int]
    positions: Mapping[str, int]


@dataclass(frozen=True)
class ValidatedInstrument:
    code: str
    option_type: str
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None
    bid_size: int | None
    ask_size: int | None
    metrics: Mapping[str, float]
    margin: float


@dataclass(frozen=True)
class ValidatedHedgeState:
    context: HedgeContext
    timestamp: object
    spot: float
    strategy_greeks: Greeks
    hedge_greeks: Greeks
    portfolio_greeks: Greeks
    delta_risk: float
    gamma_risk: float
    vega_risk: float
    delta_breached: bool
    gamma_breached: bool
    instruments: Mapping[str, ValidatedInstrument]
    margins: Mapping[str, float]
    atm_pair: tuple[float, str, str] | None
    hedge_universe: tuple[str, ...]


def build_validated_state(request, config: HedgeConfig, capital: float) -> ValidatedHedgeState:
    """Validate contracts and derive notional scenario risk for one request."""
    if not isfinite(capital) or capital <= 0:
        raise ValueError("capital must be finite and positive")
    multipliers = {item.contract_multiplier for item in request.instruments}
    if len(multipliers) != 1:
        raise ValueError("all option contract multipliers must be uniform")
    multiplier = next(iter(multipliers))
    if multiplier != config.option_multiplier:
        raise ValueError(
            f"request multiplier {multiplier} does not match hedge config "
            f"{config.option_multiplier}"
        )

    metadata = {item.instrument: item for item in request.instruments}
    alpha = {str(code): int(quantity) for code, quantity in request.confirmed_alpha_positions.items() if quantity}
    beta = {str(code): int(quantity) for code, quantity in request.confirmed_beta_positions.items() if quantity}
    held = set(alpha) | set(beta)
    missing = held - set(metadata)
    if missing:
        raise ValueError("held positions are missing instrument Greeks: " + ", ".join(sorted(missing)))
    candidates = tuple(dict.fromkeys(str(code) for code in request.hedge_universe))
    unknown = set(candidates) - set(metadata)
    if unknown:
        raise ValueError("hedge universe is missing instruments: " + ", ".join(sorted(unknown)))

    for code in set(alpha) & set(beta):
        limit = config.alpha_hedge_ratio * abs(alpha[code])
        if abs(beta[code]) > limit + 1e-9:
            raise ValueError(f"Beta position for Alpha instrument {code} exceeds 30%")

    per_contract = {
        code: InstrumentGreeks(
            code,
            Greeks(
                delta=item.delta * multiplier,
                gamma=item.gamma * multiplier,
                vega=item.vega * multiplier,
                theta=item.theta * multiplier,
            ),
        )
        for code, item in metadata.items()
    }
    strategy_greeks = aggregate_greeks(alpha, per_contract)
    hedge_greeks = aggregate_greeks(beta, per_contract)
    portfolio_greeks = strategy_greeks.plus(hedge_greeks)
    delta_risk, gamma_risk, vega_risk = scenario_risk(
        portfolio_greeks, request.spot,
        config.risk_spot_shock_fraction, config.risk_vol_shock,
    )
    delta_risk = float(delta_risk) - config.delta_center_risk
    gamma_risk = float(gamma_risk) - config.gamma_center_risk

    positions: dict[str, int] = {}
    for book in (alpha, beta):
        for code, quantity in book.items():
            positions[code] = positions.get(code, 0) + quantity
    positions = {code: quantity for code, quantity in positions.items() if quantity}

    instruments: dict[str, ValidatedInstrument] = {}
    margins: dict[str, float] = {}
    for code, item in metadata.items():
        mid = None if item.bid is None else (item.bid + item.ask) / 2.0
        if mid is not None:
            margin = short_option_margin(
                item.option_type, item.strike, mid, request.spot, multiplier
            )
        elif positions.get(code, 0) < 0:
            raise ValueError(f"live two-sided quote required to estimate short margin for {code}")
        else:
            margin = 0.0
        margins[code] = margin
        instruments[code] = ValidatedInstrument(
            code=code,
            option_type=item.option_type,
            strike=item.strike,
            bid=item.bid,
            ask=item.ask,
            mid=mid,
            bid_size=item.bid_size,
            ask_size=item.ask_size,
            metrics={
                "delta": item.delta,
                "gamma": item.gamma,
                "vega": item.vega,
                "theta": item.theta,
            },
            margin=margin,
        )

    pair_candidates: dict[float, dict[str, str]] = {}
    for code in candidates:
        item = metadata[code]
        side = "C" if item.option_type == "CALL" else "P"
        pair_candidates.setdefault(item.strike, {})[side] = code
    complete_pairs = [
        (strike, pair["C"], pair["P"])
        for strike, pair in pair_candidates.items()
        if {"C", "P"} <= pair.keys()
    ]
    atm_pair = min(complete_pairs, key=lambda pair: (abs(pair[0] - request.spot), pair[0])) if complete_pairs else None

    return ValidatedHedgeState(
        context=HedgeContext(alpha, beta, positions),
        timestamp=request.market_as_of,
        spot=request.spot,
        strategy_greeks=strategy_greeks,
        hedge_greeks=hedge_greeks,
        portfolio_greeks=portfolio_greeks,
        delta_risk=delta_risk,
        gamma_risk=gamma_risk,
        vega_risk=float(vega_risk),
        delta_breached=abs(float(delta_risk)) > config.delta_entry_risk_band,
        gamma_breached=abs(float(gamma_risk)) > config.gamma_entry_risk_band,
        instruments=instruments,
        margins=margins,
        atm_pair=atm_pair,
        hedge_universe=candidates,
    )
