"""Values shared by the application and its external adapters."""

from dataclasses import dataclass
from datetime import date, datetime
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


@dataclass(frozen=True)
class MarketQuote:
    """One normalized live quote, independent of any vendor SDK."""

    instrument: str
    observed_at: datetime
    received_at: datetime
    trading_date: date | None
    last: float | None
    bid: float | None
    ask: float | None

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        if self.last is not None and self.last <= 0:
            raise ValueError("last must be positive when present")
        if self.bid is not None and self.bid <= 0:
            raise ValueError("bid must be positive when present")
        if self.ask is not None and self.ask <= 0:
            raise ValueError("ask must be positive when present")
        if self.last is None and self.bid is None and self.ask is None:
            raise ValueError("quote must contain at least one valid price")
