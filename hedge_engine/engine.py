"""Pure Delta/Gamma hedge engine with no I/O or vendor dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import ceil, floor, isfinite
from typing import Mapping, Sequence

from hedge_engine.accounting import ALPHA_HEDGE_RATIO


_SINGULAR_TOLERANCE = 1e-12


@dataclass(frozen=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0

    def plus(self, other: "Greeks") -> "Greeks":
        return Greeks(self.delta + other.delta, self.gamma + other.gamma,
                      self.vega + other.vega, self.theta + other.theta)

    def scaled(self, quantity: float) -> "Greeks":
        return Greeks(self.delta * quantity, self.gamma * quantity,
                      self.vega * quantity, self.theta * quantity)


@dataclass(frozen=True)
class InstrumentGreeks:
    instrument: str
    greeks_per_contract: Greeks

    def __post_init__(self) -> None:
        values = self.greeks_per_contract
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if not all(isfinite(value) for value in
                   (values.delta, values.gamma, values.vega, values.theta)):
            raise ValueError("instrument Greeks must be finite")


@dataclass(frozen=True)
class HedgeDecision:
    alpha_risk: Greeks
    current_hedge_risk: Greeks
    before_hedge: Greeks
    incremental_trades: Mapping[str, float]
    after_hedge: Greeks


@dataclass(frozen=True)
class TradableHedgeDecision:
    continuous: HedgeDecision
    integer_incremental_trades: Mapping[str, int]
    after_integer_hedge: Greeks
    normalized_residual: float


def evaluate_delta_gamma_hedge(
    *, alpha_positions: Mapping[str, float],
    hedge_positions: Mapping[str, float],
    instrument_greeks: Mapping[str, InstrumentGreeks],
    hedge_universe: Sequence[str],
    alpha_hedge_ratio: float = ALPHA_HEDGE_RATIO,
    alpha_modification_penalty: float = 2.0,
    target_delta: float = 0.0,
    target_gamma: float = 0.0,
) -> HedgeDecision:
    """Solve a bounded minimum-norm, multi-instrument Delta/Gamma hedge.

    Alpha remains immutable. A trade in an Alpha instrument belongs to Beta,
    whose resulting quantity is bounded by ``alpha_hedge_ratio`` of Alpha.
    """
    universe = tuple(dict.fromkeys(str(code) for code in hedge_universe))
    if not universe:
        raise ValueError("hedge universe must not be empty")
    if (
        not isfinite(alpha_hedge_ratio)
        or not 0 <= alpha_hedge_ratio <= ALPHA_HEDGE_RATIO
    ):
        raise ValueError("alpha_hedge_ratio must be between zero and 30%")
    if not isfinite(alpha_modification_penalty) or alpha_modification_penalty < 0:
        raise ValueError("alpha_modification_penalty must not be negative")
    if not isfinite(target_delta) or not isfinite(target_gamma):
        raise ValueError("Delta/Gamma targets must be finite")

    alpha = aggregate_greeks(alpha_positions, instrument_greeks)
    current_hedge = aggregate_greeks(hedge_positions, instrument_greeks)
    before = alpha.plus(current_hedge)
    vectors = {
        code: (_lookup(code, instrument_greeks).greeks_per_contract.delta,
               _lookup(code, instrument_greeks).greeks_per_contract.gamma)
        for code in universe
    }
    if _gram_determinant(universe, vectors) < _SINGULAR_TOLERANCE:
        raise ValueError("hedge universe has a singular Delta/Gamma matrix")

    bounds: dict[str, tuple[float, float]] = {}
    for code in set(alpha_positions) & set(hedge_positions):
        limit = alpha_hedge_ratio * abs(float(alpha_positions[code]))
        if abs(float(hedge_positions[code])) > limit + _SINGULAR_TOLERANCE:
            raise ValueError(f"Beta position for Alpha instrument {code} exceeds limit")
    for code in universe:
        if code in alpha_positions:
            limit = alpha_hedge_ratio * abs(float(alpha_positions[code]))
            current = float(hedge_positions.get(code, 0.0))
            bounds[code] = (-limit - current, limit - current)

    trades = _bounded_minimum_norm_solution(
        universe,
        vectors,
        target=(target_delta - before.delta, target_gamma - before.gamma),
        bounds=bounds,
        alpha_positions=alpha_positions, hedge_positions=hedge_positions,
        alpha_modification_penalty=alpha_modification_penalty,
    )
    return HedgeDecision(alpha, current_hedge, before, trades,
                         _risk_after(before, trades, instrument_greeks))


def integerize_delta_gamma_hedge(
    decision: HedgeDecision, *,
    instrument_greeks: Mapping[str, InstrumentGreeks],
    hedge_universe: Sequence[str],
    alpha_positions: Mapping[str, float],
    hedge_positions: Mapping[str, float],
    alpha_hedge_ratio: float = ALPHA_HEDGE_RATIO,
    target_delta: float = 0.0,
    target_gamma: float = 0.0,
) -> TradableHedgeDecision:
    """Round all legs, then improve one or two legs by one contract locally."""
    if (
        not isfinite(alpha_hedge_ratio)
        or not 0 <= alpha_hedge_ratio <= ALPHA_HEDGE_RATIO
    ):
        raise ValueError("alpha_hedge_ratio must be between zero and 30%")
    if not isfinite(target_delta) or not isfinite(target_gamma):
        raise ValueError("Delta/Gamma targets must be finite")
    universe = tuple(dict.fromkeys(str(code) for code in hedge_universe))
    if set(universe) != set(decision.incremental_trades):
        raise ValueError("integer hedge universe does not match continuous decision")
    integer_bounds: dict[str, tuple[int | None, int | None]] = {}
    for code in universe:
        if code not in alpha_positions:
            integer_bounds[code] = (None, None)
        else:
            limit = alpha_hedge_ratio * abs(float(alpha_positions[code]))
            current = float(hedge_positions.get(code, 0.0))
            integer_bounds[code] = (
                ceil(-limit - current - _SINGULAR_TOLERANCE),
                floor(limit - current + _SINGULAR_TOLERANCE),
            )
    base = {
        code: _clamp_integer(round(float(decision.incremental_trades[code])),
                             *integer_bounds[code])
        for code in universe
    }
    delta_scale = max(abs(decision.before_hedge.delta - target_delta),
        *(abs(_lookup(code, instrument_greeks).greeks_per_contract.delta)
          for code in universe), _SINGULAR_TOLERANCE)
    gamma_scale = max(abs(decision.before_hedge.gamma - target_gamma),
        *(abs(_lookup(code, instrument_greeks).greeks_per_contract.gamma)
          for code in universe), _SINGULAR_TOLERANCE)

    base_residual = _risk_after(decision.before_hedge, base, instrument_greeks)
    base_distance = sum(
        abs(base[code] - decision.incremental_trades[code]) for code in universe
    )

    def evaluate(changes: tuple[tuple[str, int], ...]):
        residual = base_residual
        distance = base_distance
        for code, quantity in changes:
            adjustment = quantity - base[code]
            residual = residual.plus(
                _lookup(code, instrument_greeks).greeks_per_contract.scaled(adjustment)
            )
            distance += (
                abs(quantity - decision.incremental_trades[code])
                - abs(base[code] - decision.incremental_trades[code])
            )
        score = (
            abs(residual.delta - target_delta) / delta_scale
            + abs(residual.gamma - target_gamma) / gamma_scale
        )
        return score, distance, residual, changes

    best_result = evaluate(())

    def consider(changes: tuple[tuple[str, int], ...]) -> None:
        nonlocal best_result
        candidate = evaluate(changes)
        if candidate[:2] < best_result[:2]:
            best_result = candidate

    for code in universe:
        for adjustment in (-1, 1):
            quantity = _clamp_integer(
                base[code] + adjustment, *integer_bounds[code]
            )
            consider(((code, quantity),))
    for first, second in combinations(universe, 2):
        for first_adjustment in (-1, 1):
            for second_adjustment in (-1, 1):
                first_quantity = _clamp_integer(
                    base[first] + first_adjustment, *integer_bounds[first]
                )
                second_quantity = _clamp_integer(
                    base[second] + second_adjustment, *integer_bounds[second]
                )
                consider(((first, first_quantity), (second, second_quantity)))
    score, _, residual, changes = best_result
    best = dict(base)
    best.update(changes)
    return TradableHedgeDecision(decision, best, residual, score)


def aggregate_greeks(
    positions: Mapping[str, float],
    instrument_greeks: Mapping[str, InstrumentGreeks],
) -> Greeks:
    total = Greeks()
    for instrument, quantity in positions.items():
        total = total.plus(_lookup(instrument, instrument_greeks)
                           .greeks_per_contract.scaled(quantity))
    return total


def _bounded_minimum_norm_solution(
    universe: tuple[str, ...], vectors: Mapping[str, tuple[float, float]], *,
    target: tuple[float, float], bounds: Mapping[str, tuple[float, float]],
    alpha_positions: Mapping[str, float], hedge_positions: Mapping[str, float],
    alpha_modification_penalty: float,
) -> dict[str, float]:
    fixed: dict[str, float] = {}
    while True:
        free = tuple(code for code in universe if code not in fixed)
        residual_target = [target[0], target[1]]
        for code, quantity in fixed.items():
            delta, gamma = vectors[code]
            residual_target[0] -= delta * quantity
            residual_target[1] -= gamma * quantity
        solution = dict(fixed)
        if free:
            solution.update(_weighted_minimum_norm_solution(
                free, vectors, target=tuple(residual_target),
                alpha_positions=alpha_positions, hedge_positions=hedge_positions,
                alpha_modification_penalty=alpha_modification_penalty))
        violations = []
        for code in free:
            if code not in bounds:
                continue
            lower, upper = bounds[code]
            if solution[code] < lower:
                violations.append((lower - solution[code], code, lower))
            elif solution[code] > upper:
                violations.append((solution[code] - upper, code, upper))
        if not violations:
            return {code: solution.get(code, 0.0) for code in universe}
        _, code, boundary = max(violations)
        fixed[code] = boundary


def _weighted_minimum_norm_solution(
    free: tuple[str, ...], vectors: Mapping[str, tuple[float, float]], *,
    target: tuple[float, float], alpha_positions: Mapping[str, float],
    hedge_positions: Mapping[str, float], alpha_modification_penalty: float,
) -> dict[str, float]:
    centers: dict[str, float] = {}
    inverse_weights: dict[str, float] = {}
    adjusted = [target[0], target[1]]
    for code in free:
        if code in alpha_positions:
            weight = 1.0 + alpha_modification_penalty
            center = (-alpha_modification_penalty
                      * float(hedge_positions.get(code, 0.0)) / weight)
        else:
            weight, center = 1.0, 0.0
        centers[code], inverse_weights[code] = center, 1.0 / weight
        delta, gamma = vectors[code]
        adjusted[0] -= delta * center
        adjusted[1] -= gamma * center

    aa = ab = bb = 0.0
    for code in free:
        delta, gamma = vectors[code]
        inverse_weight = inverse_weights[code]
        aa += inverse_weight * delta * delta
        ab += inverse_weight * delta * gamma
        bb += inverse_weight * gamma * gamma
    determinant = aa * bb - ab * ab
    if determinant >= _SINGULAR_TOLERANCE:
        first = (bb * adjusted[0] - ab * adjusted[1]) / determinant
        second = (-ab * adjusted[0] + aa * adjusted[1]) / determinant
    else:
        trace = aa + bb
        if trace < _SINGULAR_TOLERANCE:
            first = second = 0.0
        else:
            # Moore-Penrose inverse of the rank-one Gram matrix.
            first = (aa * adjusted[0] + ab * adjusted[1]) / (trace * trace)
            second = (ab * adjusted[0] + bb * adjusted[1]) / (trace * trace)
    return {
        code: centers[code] + inverse_weights[code]
        * (vectors[code][0] * first + vectors[code][1] * second)
        for code in free
    }


def _gram_determinant(universe: Sequence[str],
                      vectors: Mapping[str, tuple[float, float]]) -> float:
    aa = sum(vectors[code][0] ** 2 for code in universe)
    ab = sum(vectors[code][0] * vectors[code][1] for code in universe)
    bb = sum(vectors[code][1] ** 2 for code in universe)
    return aa * bb - ab * ab


def _risk_after(before: Greeks, trades: Mapping[str, float],
                instrument_greeks: Mapping[str, InstrumentGreeks]) -> Greeks:
    result = before
    for code, quantity in trades.items():
        result = result.plus(_lookup(code, instrument_greeks)
                             .greeks_per_contract.scaled(quantity))
    return result


def _clamp_integer(value: int, lower: int | None, upper: int | None) -> int:
    if lower is not None:
        value = max(value, lower)
    if upper is not None:
        value = min(value, upper)
    return value


def _lookup(instrument: str,
            values: Mapping[str, InstrumentGreeks]) -> InstrumentGreeks:
    try:
        return values[instrument]
    except KeyError as exc:
        raise ValueError(f"missing Greeks for {instrument}") from exc
