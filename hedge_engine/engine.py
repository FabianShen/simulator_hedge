"""Pure Delta/Gamma hedge engine with no I/O or vendor dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Mapping


@dataclass(frozen=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0

    def plus(self, other: "Greeks") -> "Greeks":
        return Greeks(
            self.delta + other.delta,
            self.gamma + other.gamma,
            self.vega + other.vega,
            self.theta + other.theta,
        )

    def scaled(self, quantity: float) -> "Greeks":
        return Greeks(
            self.delta * quantity,
            self.gamma * quantity,
            self.vega * quantity,
            self.theta * quantity,
        )


@dataclass(frozen=True)
class InstrumentGreeks:
    instrument: str
    greeks_per_contract: Greeks

    def __post_init__(self) -> None:
        values = self.greeks_per_contract
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if not all(
            isfinite(value)
            for value in (values.delta, values.gamma, values.vega, values.theta)
        ):
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
    *,
    alpha_positions: Mapping[str, float],
    hedge_positions: Mapping[str, float],
    instrument_greeks: Mapping[str, InstrumentGreeks],
    hedge_pair: tuple[str, str],
) -> HedgeDecision:
    """Solve continuous incremental trades that neutralize Delta and Gamma."""

    alpha = _aggregate(alpha_positions, instrument_greeks)
    current_hedge = _aggregate(hedge_positions, instrument_greeks)
    before = alpha.plus(current_hedge)
    first, second = hedge_pair
    first_greeks = _lookup(first, instrument_greeks).greeks_per_contract
    second_greeks = _lookup(second, instrument_greeks).greeks_per_contract
    determinant = (
        first_greeks.delta * second_greeks.gamma
        - second_greeks.delta * first_greeks.gamma
    )
    if abs(determinant) < 1e-12:
        raise ValueError("hedge pair has a singular Delta/Gamma matrix")
    first_quantity = (
        -before.delta * second_greeks.gamma
        + second_greeks.delta * before.gamma
    ) / determinant
    second_quantity = (
        first_greeks.gamma * before.delta
        - first_greeks.delta * before.gamma
    ) / determinant
    trades = {first: first_quantity, second: second_quantity}
    after = before.plus(first_greeks.scaled(first_quantity)).plus(
        second_greeks.scaled(second_quantity)
    )
    return HedgeDecision(
        alpha_risk=alpha,
        current_hedge_risk=current_hedge,
        before_hedge=before,
        incremental_trades=trades,
        after_hedge=after,
    )


def integerize_delta_gamma_hedge(
    decision: HedgeDecision,
    *,
    instrument_greeks: Mapping[str, InstrumentGreeks],
    hedge_pair: tuple[str, str],
) -> TradableHedgeDecision:
    """Choose the best tradable combination surrounding the continuous solution."""

    first, second = hedge_pair
    first_greeks = _lookup(first, instrument_greeks).greeks_per_contract
    second_greeks = _lookup(second, instrument_greeks).greeks_per_contract
    first_target = decision.incremental_trades[first]
    second_target = decision.incremental_trades[second]
    delta_scale = max(
        abs(decision.before_hedge.delta),
        abs(first_greeks.delta),
        abs(second_greeks.delta),
        1e-12,
    )
    gamma_scale = max(
        abs(decision.before_hedge.gamma),
        abs(first_greeks.gamma),
        abs(second_greeks.gamma),
        1e-12,
    )
    candidates = (
        (first_quantity, second_quantity)
        for first_quantity in {floor(first_target), ceil(first_target)}
        for second_quantity in {floor(second_target), ceil(second_target)}
    )

    def evaluate(candidate: tuple[int, int]) -> tuple[float, float, int, int, Greeks]:
        first_quantity, second_quantity = candidate
        residual = decision.before_hedge.plus(
            first_greeks.scaled(first_quantity)
        ).plus(second_greeks.scaled(second_quantity))
        score = abs(residual.delta) / delta_scale + abs(residual.gamma) / gamma_scale
        distance = abs(first_quantity - first_target) + abs(
            second_quantity - second_target
        )
        return score, distance, first_quantity, second_quantity, residual

    score, _, first_quantity, second_quantity, residual = min(
        (evaluate(candidate) for candidate in candidates),
        key=lambda value: value[:4],
    )
    return TradableHedgeDecision(
        continuous=decision,
        integer_incremental_trades={
            first: first_quantity,
            second: second_quantity,
        },
        after_integer_hedge=residual,
        normalized_residual=score,
    )


def _aggregate(
    positions: Mapping[str, float],
    instrument_greeks: Mapping[str, InstrumentGreeks],
) -> Greeks:
    total = Greeks()
    for instrument, quantity in positions.items():
        total = total.plus(
            _lookup(instrument, instrument_greeks).greeks_per_contract.scaled(quantity)
        )
    return total


def _lookup(
    instrument: str, values: Mapping[str, InstrumentGreeks]
) -> InstrumentGreeks:
    try:
        return values[instrument]
    except KeyError as exc:
        raise ValueError(f"missing Greeks for {instrument}") from exc
