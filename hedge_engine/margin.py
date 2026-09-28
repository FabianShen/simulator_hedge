"""Live short-option margin estimate used by hedge feasibility checks."""

from math import isfinite


def short_option_margin(
    option_type: str,
    strike: float,
    premium: float,
    spot: float,
    multiplier: int,
) -> float:
    """Estimate margin per short contract from the current premium and spot."""
    option_type = str(option_type).upper()
    if option_type not in {"CALL", "PUT", "C", "P"}:
        raise ValueError("option_type must be CALL or PUT")
    if not all(isfinite(float(value)) for value in (strike, premium, spot)):
        raise ValueError("margin inputs must be finite")
    if strike <= 0 or premium < 0 or spot <= 0 or multiplier <= 0:
        raise ValueError("strike, spot, and multiplier must be positive; premium nonnegative")
    if option_type in {"CALL", "C"}:
        amount = premium + max(0.12 * spot - max(strike - spot, 0.0), 0.07 * spot)
    else:
        amount = min(
            premium + max(0.12 * spot - max(spot - strike, 0.0), 0.07 * strike),
            strike,
        )
    return float(amount * multiplier)
