"""Interfaces owned by the application, not by external SDKs."""

from typing import Protocol

from sim_hedge.domain import MarketQuote


class MarketDataSource(Protocol):
    """The market-data behavior needed by the application."""

    def run(self, on_quote: "QuoteHandler") -> None:
        """Block while normalized live quotes are delivered to ``on_quote``."""

    def stop(self) -> None:
        """Request a clean stop."""


class QuoteHandler(Protocol):
    def __call__(self, quote: MarketQuote) -> None:
        """Consume one normalized live quote."""
