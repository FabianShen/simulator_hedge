"""Pricing interface owned by the application, not by an external SDK."""

from typing import Any, Mapping, Protocol

from sim_hedge.domain.pricing import PricingBatch, PricingServiceHealth


class PricingClient(Protocol):
    """Transport-neutral external pricing behavior needed by the application."""

    def health(self) -> PricingServiceHealth:
        """Verify service and protocol compatibility."""

    def price(self, request: Mapping[str, Any]) -> PricingBatch:
        """Price one immutable, versioned request."""

    def close(self) -> None:
        """Release transport resources."""
