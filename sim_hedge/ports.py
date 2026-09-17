"""Interfaces owned by the application, not by external SDKs."""

from typing import Protocol

from datetime import date

from sim_hedge.domain import MarketQuote, OptionContract


class MarketDataSource(Protocol):
    """The market-data behavior needed by the application."""

    def run(self, on_quote: "QuoteHandler") -> None:
        """Block while normalized live quotes are delivered to ``on_quote``."""

    def stop(self) -> None:
        """Request a clean stop."""


class QuoteHandler(Protocol):
    def __call__(self, quote: MarketQuote) -> None:
        """Consume one normalized live quote."""


class OptionReferenceSource(Protocol):
    def load_option_chain(
        self,
        underlying: str,
        trading_date: date,
    ) -> list[OptionContract]:
        """Return active option contracts for one underlying and trading date."""
