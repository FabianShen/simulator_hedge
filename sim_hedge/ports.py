"""Interfaces owned by the application, not by external SDKs."""

from typing import Any, Mapping, Protocol

from datetime import date

from sim_hedge.domain import MarketQuote, OptionContract
from sim_hedge.portfolio import PortfolioSnapshot
from sim_hedge.pricing_types import PricingBatch, PricingServiceHealth


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


class PricingClient(Protocol):
    """Transport-neutral external pricing behavior needed by the application."""

    def health(self) -> PricingServiceHealth:
        """Verify service and protocol compatibility."""

    def price(self, request: Mapping[str, Any]) -> PricingBatch:
        """Price one immutable, versioned request."""

    def close(self) -> None:
        """Release transport resources."""


class PortfolioSource(Protocol):
    """Read-only authoritative portfolio snapshot source."""

    def load(self, account_id: str) -> PortfolioSnapshot:
        """Load one absolute account snapshot suitable for state replacement."""
