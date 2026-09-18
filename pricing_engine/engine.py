"""Pure offline orchestration for SABR calibration, pricing, and Greeks."""

from dataclasses import dataclass
from math import exp, isfinite

from pricing_engine.black76 import black76_price, implied_volatility
from pricing_engine.sabr import (
    SabrCalibration,
    SabrParameters,
    calibrate_sabr,
    sabr_volatility,
)


@dataclass(frozen=True)
class OptionObservation:
    instrument: str
    option_type: str
    strike: float
    market_price: float

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if self.option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be 'CALL' or 'PUT'")
        if self.strike <= 0 or self.market_price <= 0:
            raise ValueError("strike and market_price must be positive")


@dataclass(frozen=True)
class SabrPricingRequest:
    spot: float
    time_to_expiry: float
    rate: float
    dividend_yield: float
    options: tuple[OptionObservation, ...]
    beta: float = 0.5
    minimum_strikes: int = 3

    def __post_init__(self) -> None:
        values = (self.spot, self.time_to_expiry, self.rate, self.dividend_yield)
        if not all(isfinite(value) for value in values):
            raise ValueError("request numbers must be finite")
        if self.spot <= 0 or self.time_to_expiry <= 0:
            raise ValueError("spot and time_to_expiry must be positive")
        if not self.options:
            raise ValueError("at least one option observation is required")
        if not 0 <= self.beta <= 1:
            raise ValueError("beta must be between zero and one")
        if self.minimum_strikes < 3:
            raise ValueError("minimum_strikes must be at least three")


@dataclass(frozen=True)
class OptionPricingResult:
    instrument: str
    status: str
    error: str | None = None
    market_implied_volatility: float | None = None
    model_implied_volatility: float | None = None
    theoretical_price: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta_per_year: float | None = None
    vega_per_absolute_volatility: float | None = None
    rho_per_absolute_rate: float | None = None


@dataclass(frozen=True)
class SabrPricingResponse:
    calibration: SabrCalibration
    results: tuple[OptionPricingResult, ...]


class SabrPricingEngine:
    """Reference engine with no files, network, SDK, clock, or global state."""

    def price(self, request: SabrPricingRequest) -> SabrPricingResponse:
        forward = request.spot * exp(
            (request.rate - request.dividend_yield) * request.time_to_expiry
        )
        market_volatilities: dict[str, float] = {}
        calibration_points: list[tuple[float, float]] = []
        errors: dict[str, str] = {}

        for option in request.options:
            try:
                volatility = implied_volatility(
                    forward,
                    option.strike,
                    request.time_to_expiry,
                    request.rate,
                    option.market_price,
                    option.option_type,
                )
            except ValueError as exc:
                errors[option.instrument] = str(exc)
                continue
            market_volatilities[option.instrument] = volatility
            calibration_points.append((option.strike, volatility))

        calibration = calibrate_sabr(
            forward,
            request.time_to_expiry,
            calibration_points,
            beta=request.beta,
            minimum_strikes=request.minimum_strikes,
        )
        results = tuple(
            self._price_option(
                request,
                option,
                calibration.parameters,
                market_volatilities.get(option.instrument),
                errors.get(option.instrument),
            )
            for option in request.options
        )
        return SabrPricingResponse(calibration=calibration, results=results)

    def _price_option(
        self,
        request: SabrPricingRequest,
        option: OptionObservation,
        parameters: SabrParameters,
        market_volatility: float | None,
        error: str | None,
    ) -> OptionPricingResult:
        if error is not None:
            return OptionPricingResult(
                instrument=option.instrument,
                status="INVALID_INPUT",
                error=error,
            )

        price, model_volatility = _model_price(
            request.spot,
            option.strike,
            request.time_to_expiry,
            option.option_type,
            request.rate,
            request.dividend_yield,
            parameters,
        )
        greeks = _finite_difference_greeks(
            request,
            option,
            parameters,
            price,
            model_volatility,
        )
        return OptionPricingResult(
            instrument=option.instrument,
            status="OK",
            market_implied_volatility=market_volatility,
            model_implied_volatility=model_volatility,
            theoretical_price=price,
            **greeks,
        )


def _model_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    option_type: str,
    rate: float,
    dividend_yield: float,
    parameters: SabrParameters,
) -> tuple[float, float]:
    forward = spot * exp((rate - dividend_yield) * time_to_expiry)
    volatility = sabr_volatility(forward, strike, time_to_expiry, parameters)
    price = black76_price(forward, strike, time_to_expiry, rate, volatility, option_type)
    return price, volatility


def _finite_difference_greeks(
    request: SabrPricingRequest,
    option: OptionObservation,
    parameters: SabrParameters,
    base_price: float,
    model_volatility: float,
) -> dict[str, float]:
    """Using finite difference calculation for SABR Greeks"""
    spot_bump = max(request.spot * 1e-4, 1e-6)
    price_up = _model_price(
        request.spot + spot_bump,
        option.strike,
        request.time_to_expiry,
        option.option_type,
        request.rate,
        request.dividend_yield,
        parameters,
    )[0]
    price_down = _model_price(
        request.spot - spot_bump,
        option.strike,
        request.time_to_expiry,
        option.option_type,
        request.rate,
        request.dividend_yield,
        parameters,
    )[0]
    delta = (price_up - price_down) / (2.0 * spot_bump)
    gamma = (price_up - 2.0 * base_price + price_down) / spot_bump**2

    forward = request.spot * exp(
        (request.rate - request.dividend_yield) * request.time_to_expiry
    )
    volatility_bump = max(model_volatility * 1e-4, 1e-6)
    price_vol_up = black76_price(
        forward,
        option.strike,
        request.time_to_expiry,
        request.rate,
        model_volatility + volatility_bump,
        option.option_type,
    )
    price_vol_down = black76_price(
        forward,
        option.strike,
        request.time_to_expiry,
        request.rate,
        max(model_volatility - volatility_bump, 1e-8),
        option.option_type,
    )
    vega = (price_vol_up - price_vol_down) / (
        model_volatility + volatility_bump
        - max(model_volatility - volatility_bump, 1e-8)
    )

    time_bump = min(max(request.time_to_expiry * 1e-4, 1e-7), request.time_to_expiry * 0.25)
    price_later = _model_price(
        request.spot,
        option.strike,
        request.time_to_expiry - time_bump,
        option.option_type,
        request.rate,
        request.dividend_yield,
        parameters,
    )[0]
    price_earlier = _model_price(
        request.spot,
        option.strike,
        request.time_to_expiry + time_bump,
        option.option_type,
        request.rate,
        request.dividend_yield,
        parameters,
    )[0]
    theta = (price_later - price_earlier) / (2.0 * time_bump)

    rate_bump = 1e-5
    price_rate_up = _model_price(
        request.spot,
        option.strike,
        request.time_to_expiry,
        option.option_type,
        request.rate + rate_bump,
        request.dividend_yield,
        parameters,
    )[0]
    price_rate_down = _model_price(
        request.spot,
        option.strike,
        request.time_to_expiry,
        option.option_type,
        request.rate - rate_bump,
        request.dividend_yield,
        parameters,
    )[0]
    rho = (price_rate_up - price_rate_down) / (2.0 * rate_bump)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta_per_year": theta,
        "vega_per_absolute_volatility": vega,
        "rho_per_absolute_rate": rho,
    }
