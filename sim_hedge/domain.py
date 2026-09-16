"""Core values used by the simulator.

Domain values contain data, not orchestration or external I/O.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketTick:
    """The market price observed at one simulation step."""

    step: int
    spot: float

    def __post_init__(self) -> None:
        if self.step < 0:
            raise ValueError("step must be non-negative")
        if self.spot <= 0:
            raise ValueError("spot must be positive")

