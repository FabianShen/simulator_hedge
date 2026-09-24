"""Normalized option-contract values independent of external vendors."""

from dataclasses import dataclass
from datetime import date
from enum import Enum


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True)
class OptionContract:
    """Static option metadata needed for subscriptions and pricing."""

    instrument: str
    underlying: str
    option_type: OptionType
    strike: float
    maturity: date
    contract_multiplier: int
    price_tick: float

    def __post_init__(self) -> None:
        if not self.instrument or not self.underlying:
            raise ValueError("instrument and underlying must not be empty")
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.contract_multiplier <= 0:
            raise ValueError("contract_multiplier must be positive")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")
