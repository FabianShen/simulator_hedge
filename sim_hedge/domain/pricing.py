"""Vendor- and transport-neutral pricing service results."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PricingServiceHealth:
    engine_name: str
    engine_version: str
    protocol_version: str


@dataclass(frozen=True)
class SabrFit:
    forward: float
    alpha: float
    beta: float
    nu: float
    rho: float
    rmse: float
    valid_strikes: int


@dataclass(frozen=True)
class OptionValuation:
    instrument: str
    status: str
    error: str | None
    theoretical_price: float | None
    market_implied_volatility: float | None
    model_implied_volatility: float | None
    implied_volatility_error: float | None
    delta: float | None
    gamma: float | None
    theta_per_year: float | None
    vega_per_absolute_volatility: float | None
    rho_per_absolute_rate: float | None


@dataclass(frozen=True)
class PricingBatch:
    request_id: str
    calculated_at: datetime
    engine_name: str
    engine_version: str
    model: str
    calibration: SabrFit
    results: tuple[OptionValuation, ...]
