"""Small Black-76 implementation used by the reference SABR engine."""

from math import erf, exp, isfinite, log, sqrt
from scipy.optimize import brentq

def black76_price(
    forward: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    volatility: float,
    option_type: str,
) -> float:
    _validate_option_type(option_type)
    values = (forward, strike, time_to_expiry, rate, volatility)
    if not all(isfinite(value) for value in values):
        raise ValueError("Black-76 inputs must be finite")
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")
    if time_to_expiry < 0 or volatility < 0:
        raise ValueError("time and volatility must not be negative")

    discount = exp(-rate * time_to_expiry)
    if time_to_expiry == 0 or volatility == 0:
        return discount * _intrinsic(forward, strike, option_type)

    root_time = sqrt(time_to_expiry)
    d1 = (
        log(forward / strike)
        + 0.5 * volatility * volatility * time_to_expiry
    ) / (volatility * root_time)
    d2 = d1 - volatility * root_time
    if option_type == "CALL":
        return discount * (forward * _normal_cdf(d1) - strike * _normal_cdf(d2))
    return discount * (strike * _normal_cdf(-d2) - forward * _normal_cdf(-d1))


def implied_volatility(
    forward: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    market_price: float,
    option_type: str,
    *,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
) -> float:
    """Solve Black-76 implied volatility with deterministic bisection."""

    _validate_option_type(option_type)
    if time_to_expiry <= 0:
        raise ValueError("time_to_expiry must be positive for implied volatility")
    if market_price < 0 or not isfinite(market_price):
        raise ValueError("market_price must be finite and non-negative")

    discount = exp(-rate * time_to_expiry)
    lower_price = discount * _intrinsic(forward, strike, option_type)
    upper_price = discount * (forward if option_type == "CALL" else strike)
    if not lower_price < market_price < upper_price:
        raise ValueError("market_price is outside no-arbitrage bounds")

    def objective(vol):
        return (
            black76_price(
                forward,
                strike,
                time_to_expiry,
                rate,
                vol,
                option_type,
            )
            - market_price
        )

    return brentq(
        objective,
        a=1e-8,
        b=5.0,
        xtol=tolerance,
        rtol=1e-12,
        maxiter=max_iterations,
    )


def _intrinsic(forward: float, strike: float, option_type: str) -> float:
    if option_type == "CALL":
        return max(forward - strike, 0.0)
    return max(strike - forward, 0.0)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _validate_option_type(option_type: str) -> None:
    if option_type not in {"CALL", "PUT"}:
        raise ValueError("option_type must be 'CALL' or 'PUT'")
