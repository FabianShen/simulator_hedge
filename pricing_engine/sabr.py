"""Hagan-style SABR smile and a simple deterministic offline calibrator."""

from dataclasses import dataclass
from math import isfinite, log, sqrt
from statistics import fmean, median


@dataclass(frozen=True)
class SabrParameters:
    alpha: float
    beta: float
    nu: float
    rho: float

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.nu <= 0:
            raise ValueError("alpha and nu must be positive")
        if not 0 <= self.beta <= 1:
            raise ValueError("beta must be between zero and one")
        if not -1 < self.rho < 1:
            raise ValueError("rho must be strictly between -1 and 1")


@dataclass(frozen=True)
class SabrCalibration:
    forward: float
    parameters: SabrParameters
    rmse: float
    valid_strikes: int


def sabr_volatility(
    forward: float,
    strike: float,
    time_to_expiry: float,
    parameters: SabrParameters,
) -> float:
    """Return a lognormal SABR IV approximation."""

    if forward <= 0 or strike <= 0 or time_to_expiry < 0:
        raise ValueError("forward/strike must be positive and time non-negative")

    alpha = parameters.alpha
    beta = parameters.beta
    nu = parameters.nu
    rho = parameters.rho
    one_minus_beta = 1.0 - beta
    log_fk = log(forward / strike)

    if abs(one_minus_beta) < 1e-12:
        distance = log_fk
    else:
        distance = (
            forward ** one_minus_beta - strike ** one_minus_beta
        ) / one_minus_beta

    z = (nu / alpha) * distance
    if abs(z) <= 1e-10:
        z_over_x = 1.0
    else:
        radicand = max(1.0 - 2.0 * rho * z + z * z, 0.0)
        ratio = (sqrt(radicand) + z - rho) / (1.0 - rho)
        if ratio <= 0:
            raise FloatingPointError("SABR logarithm is outside its domain")
        x_z = log(ratio)
        z_over_x = z / x_z if abs(x_z) > 1e-12 else 1.0

    if abs(log_fk) <= 1e-10 or abs(distance) <= 1e-14:
        base = alpha / (forward ** one_minus_beta)
    else:
        base = alpha * log_fk / distance

    fk_scale = (forward * strike) ** (one_minus_beta / 2.0)
    correction = 1.0 + time_to_expiry * (
        one_minus_beta**2 * alpha**2 / (24.0 * fk_scale**2)
        + 0.25 * rho * beta * nu * alpha / fk_scale
        + (2.0 - 3.0 * rho**2) * nu**2 / 24.0
    )
    result = base * z_over_x * correction
    if not isfinite(result) or result <= 0:
        raise FloatingPointError("SABR produced an invalid volatility")
    return result


def calibrate_sabr(
    forward: float,
    time_to_expiry: float,
    observations: list[tuple[float, float]],
    *,
    beta: float = 0.5,
) -> SabrCalibration:
    """Fit alpha/nu/rho to strike-IV observations without SciPy.

    This reference calibrator fixes alpha from the nearest-to-forward IV and
    performs a deterministic coarse-to-fine search over nu and rho. It favors
    clarity and repeatability over production calibration speed or precision.
    """

    if forward <= 0 or time_to_expiry <= 0:
        raise ValueError("forward and time_to_expiry must be positive")
    if not 0 <= beta <= 1:
        raise ValueError("beta must be between zero and one")

    by_strike: dict[float, list[float]] = {}
    for strike, volatility in observations:
        if strike > 0 and volatility > 0 and isfinite(strike) and isfinite(volatility):
            by_strike.setdefault(strike, []).append(volatility)
    points = sorted(
        (strike, median(volatilities))
        for strike, volatilities in by_strike.items()
    )
    if len(points) < 3:
        raise ValueError("at least three valid unique strikes are required")

    atm_strike, atm_volatility = min(
        points, key=lambda point: abs(point[0] - forward)
    )
    del atm_strike
    alpha = atm_volatility * forward ** (1.0 - beta)

    best_nu = 0.5
    best_rho = -0.2
    best_loss = float("inf")
    nu_span = 1.0
    rho_span = 0.8

    for _ in range(7):
        center_nu = best_nu
        center_rho = best_rho
        for nu_step in range(-4, 5):
            nu = max(0.001, center_nu + nu_step * nu_span / 4.0)
            for rho_step in range(-4, 5):
                rho = min(
                    0.999,
                    max(-0.999, center_rho + rho_step * rho_span / 4.0),
                )
                parameters = SabrParameters(alpha, beta, nu, rho)
                errors = [
                    sabr_volatility(forward, strike, time_to_expiry, parameters)
                    - market_volatility
                    for strike, market_volatility in points
                ]
                loss = fmean(error * error for error in errors)
                if loss < best_loss:
                    best_loss = loss
                    best_nu = nu
                    best_rho = rho
        nu_span /= 3.0
        rho_span /= 3.0

    return SabrCalibration(
        forward=forward,
        parameters=SabrParameters(alpha, beta, best_nu, best_rho),
        rmse=sqrt(best_loss),
        valid_strikes=len(points),
    )
