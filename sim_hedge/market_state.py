"""Thread-safe, bounded state for the latest live market quotes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Iterable

from sim_hedge.domain import MarketQuote


@dataclass(frozen=True)
class MarketReadiness:
    """Described Market State safty and readability"""

    ready: bool
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    feed_unsafe: bool


class MarketState:
    """Keep one latest quote per required instrument."""

    def __init__(self, required_instruments: Iterable[str]) -> None:
        required = tuple(dict.fromkeys(required_instruments))
        if not required or any(not instrument for instrument in required):
            raise ValueError("at least one non-empty required instrument is needed")

        self._required = required
        self._quotes: dict[str, MarketQuote] = {}
        self._lock = Lock()

    def apply_quote(self, quote: MarketQuote) -> None:
        """Replace the latest quote for one instrument atomically."""

        with self._lock:
            self._quotes[quote.instrument] = quote

    def latest(self, instrument: str) -> MarketQuote | None:
        with self._lock:
            return self._quotes.get(instrument)

    def snapshot(self) -> dict[str, MarketQuote]:
        """Return all latest quotes."""

        with self._lock:
            return dict(self._quotes)

    def readiness(
        self,
        *,
        now: datetime,
        max_age: timedelta,
        feed_unsafe: bool = False,
    ) -> MarketReadiness:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        quotes = self.snapshot()
        missing = tuple(code for code in self._required if code not in quotes)
        stale = tuple(
            code
            for code in self._required
            if code in quotes and now - quotes[code].received_at > max_age
        )
        return MarketReadiness(
            ready=not feed_unsafe and not missing and not stale,
            missing=missing,
            stale=stale,
            feed_unsafe=feed_unsafe,
        )
