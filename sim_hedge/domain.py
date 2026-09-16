"""Values shared by the application and its external adapters."""

from dataclasses import dataclass
from datetime import date, datetime


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
        if self.last is not None and self.last <= 0:
            raise ValueError("last must be positive when present")
        if self.bid is not None and self.bid <= 0:
            raise ValueError("bid must be positive when present")
        if self.ask is not None and self.ask <= 0:
            raise ValueError("ask must be positive when present")
        if self.last is None and self.bid is None and self.ask is None:
            raise ValueError("quote must contain at least one valid price")
