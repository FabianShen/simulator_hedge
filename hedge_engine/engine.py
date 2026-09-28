"""Pure Greeks values and aggregation; policy solvers live in separate modules."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping


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
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        values = self.greeks_per_contract
        if not all(isfinite(value) for value in
                   (values.delta, values.gamma, values.vega, values.theta)):
            raise ValueError("instrument Greeks must be finite")


def aggregate_greeks(
    positions: Mapping[str, float],
    instrument_greeks: Mapping[str, InstrumentGreeks],
) -> Greeks:
    total = Greeks()
    for instrument, quantity in positions.items():
        total = total.plus(_lookup(instrument, instrument_greeks)
                           .greeks_per_contract.scaled(quantity))
    return total


def _lookup(
    instrument: str, values: Mapping[str, InstrumentGreeks]
) -> InstrumentGreeks:
    try:
        return values[instrument]
    except KeyError as exc:
        raise ValueError(f"missing Greeks for {instrument}") from exc
