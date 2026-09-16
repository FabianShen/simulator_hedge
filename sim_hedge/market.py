"""Deterministic market-data generation."""

from collections.abc import Iterable, Iterator

from sim_hedge.domain import MarketTick


def generate_market_ticks(
    initial_spot: float,
    price_changes: Iterable[float],
) -> Iterator[MarketTick]:
    """Yield one initial tick followed by one tick per absolute price change.

    Absolute changes keep the first model easy to inspect. For example, an
    initial spot of 100 with changes (+1, -0.5) produces 100, 101, 100.5.
    """

    tick = MarketTick(step=0, spot=initial_spot)
    yield tick

    spot = initial_spot
    for step, change in enumerate(price_changes, start=1):
        spot += change
        yield MarketTick(step=step, spot=spot)

