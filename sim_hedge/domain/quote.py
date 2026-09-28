"""Normalized market quotes independent of external vendors."""

from dataclasses import dataclass
from datetime import date, datetime
from numbers import Integral


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
    previous_close: float | None = None
    previous_settlement: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None

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
        if self.previous_close is not None and self.previous_close <= 0:
            raise ValueError("previous_close must be positive when present")
        if self.previous_settlement is not None and self.previous_settlement <= 0:
            raise ValueError("previous_settlement must be positive when present")
        for name in ("bid_size", "ask_size"):
            size = getattr(self, name)
            if size is not None and (
                isinstance(size, bool) or not isinstance(size, Integral) or size < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer when present")
        if self.last is None and self.bid is None and self.ask is None:
            raise ValueError("quote must contain at least one valid price")
