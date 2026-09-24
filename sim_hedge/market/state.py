"""Thread-safe, bounded state for the latest live market quotes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Iterable

from sim_hedge.domain.quote import MarketQuote


@dataclass(frozen=True)
class MarketReadiness:
    """Described Market State safty and readability"""

    ready: bool
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    feed_unsafe: bool


class MarketState:
    """Keep one latest quote per required instrument."""

    def __init__(self, instruments: Iterable[str]) -> None:
        required = tuple(dict.fromkeys(instruments))
        if not required or any(not instrument for instrument in required):
            raise ValueError("at least one non-empty required instrument is needed")

        self._required = required
        self._quotes: dict[str, MarketQuote] = {}
        self._lock = Lock()

    @property
    def required_instruments(self) -> tuple[str, ...]:
        with self._lock:
            return self._required

    def set_required(self, instruments: Iterable[str]) -> None:
        """Replace the instruments that must be fresh before pricing can run."""

        required = tuple(dict.fromkeys(instruments))
        if not required or any(not instrument for instrument in required):
            raise ValueError("at least one non-empty required instrument is needed")
        with self._lock:
            self._required = required

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

        with self._lock:
            quotes = dict(self._quotes)
            required = self._required
        missing = tuple(code for code in required if code not in quotes)
        stale = tuple(
            code
            for code in required
            if code in quotes and now - quotes[code].received_at > max_age
        )
        return MarketReadiness(
            ready=not feed_unsafe and not missing and not stale,
            missing=missing,
            stale=stale,
            feed_unsafe=feed_unsafe,
        )
