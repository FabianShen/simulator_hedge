"""Validated inputs shared by hedge policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping, Sequence

from hedge_engine.engine import InstrumentGreeks


@dataclass(frozen=True)
class MarketSnapshot:
    as_of: datetime
    spot: float
    spot_observed_at: datetime
    marks: Mapping[str, float]
    observed_at: Mapping[str, datetime]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("market as_of must be timezone-aware")
        if not isfinite(self.spot) or self.spot <= 0:
            raise ValueError("market spot must be finite and positive")
        if self.spot_observed_at.tzinfo is None:
            raise ValueError("spot observation must be timezone-aware")


@dataclass(frozen=True)
class HedgeContext:
    """One reconciled, immutable-by-convention hedge decision boundary."""

    market: MarketSnapshot
    alpha_positions: Mapping[str, int]
    beta_positions: Mapping[str, int]
    broker_positions: Mapping[str, int]
    strategy_universe: tuple[str, ...]
    hedge_universe: tuple[str, ...]
    instrument_greeks: Mapping[str, InstrumentGreeks]


def build_hedge_context(
    *,
    market: MarketSnapshot,
    alpha_positions: Mapping[str, int],
    beta_positions: Mapping[str, int],
    broker_positions: Mapping[str, int],
    strategy_universe: Sequence[str],
    instrument_greeks: Mapping[str, InstrumentGreeks],
    max_market_age_seconds: float = 10.0,
) -> HedgeContext:
    """Reconcile books and construct the Alpha-excluding hedge universe."""

    if max_market_age_seconds <= 0:
        raise ValueError("max_market_age_seconds must be positive")
    spot_age = (market.as_of - market.spot_observed_at).total_seconds()
    if spot_age < 0 or spot_age > max_market_age_seconds:
        raise ValueError(f"stale spot observation: age={spot_age:g}s")
    alpha = _positions(alpha_positions, "Alpha")
    beta = _positions(beta_positions, "Beta")
    broker = _positions(broker_positions, "broker")
    overlap = set(alpha) & set(beta)
    if overlap:
        raise ValueError(
            "Alpha and Beta books must use disjoint instruments: "
            + ", ".join(sorted(overlap))
        )
    expected = _net(alpha, beta)
    if broker != expected:
        raise ValueError(
            f"broker positions do not reconcile: expected {expected}, received {broker}"
        )

    universe = tuple(dict.fromkeys(str(value) for value in strategy_universe))
    if not universe:
        raise ValueError("strategy universe must not be empty")
    hedge_universe = tuple(code for code in universe if code not in alpha)
    if not hedge_universe:
        raise ValueError("Alpha positions consume the entire strategy universe")
    outside = set(beta) - set(hedge_universe)
    if outside:
        raise ValueError(
            "Beta positions are outside the allowed hedge universe: "
            + ", ".join(sorted(outside))
        )

    required = set(expected) | set(hedge_universe)
    missing_greeks = required - set(instrument_greeks)
    if missing_greeks:
        raise ValueError("missing pricing Greeks: " + ", ".join(sorted(missing_greeks)))
    missing_market = required - set(market.marks)
    if missing_market:
        raise ValueError("missing market marks: " + ", ".join(sorted(missing_market)))
    missing_times = required - set(market.observed_at)
    if missing_times:
        raise ValueError(
            "missing market observation times: " + ", ".join(sorted(missing_times))
        )
    for code in required:
        mark = float(market.marks[code])
        observed = market.observed_at[code]
        if not isfinite(mark) or mark <= 0:
            raise ValueError(f"invalid market mark for {code}")
        if observed.tzinfo is None:
            raise ValueError(f"market observation for {code} must be timezone-aware")
        age = (market.as_of - observed).total_seconds()
        if age < 0 or age > max_market_age_seconds:
            raise ValueError(f"stale market observation for {code}: age={age:g}s")

    return HedgeContext(
        market=market,
        alpha_positions=alpha,
        beta_positions=beta,
        broker_positions=broker,
        strategy_universe=universe,
        hedge_universe=hedge_universe,
        instrument_greeks=dict(instrument_greeks),
    )


def _positions(values: Mapping[str, int], name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw_code, raw_quantity in values.items():
        code = str(raw_code)
        if not code:
            raise ValueError(f"{name} position instrument must not be empty")
        if isinstance(raw_quantity, bool) or int(raw_quantity) != raw_quantity:
            raise ValueError(f"{name} position for {code} must be an integer")
        quantity = int(raw_quantity)
        if quantity:
            result[code] = quantity
    return result


def _net(*books: Mapping[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for book in books:
        for code, quantity in book.items():
            result[code] = result.get(code, 0) + quantity
    return {code: quantity for code, quantity in result.items() if quantity}
