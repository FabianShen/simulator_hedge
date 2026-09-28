"""Risk and solver settings for the reference hedge policies."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from hedge_engine.accounting import ALPHA_HEDGE_RATIO


@dataclass(frozen=True)
class HedgeConfig:
    option_multiplier: int = 10_000
    option_fee: float = 2.0
    risk_spot_shock_fraction: float = 0.01
    risk_vol_shock: float = 0.01
    margin_limit_fraction: float = 0.70
    alpha_hedge_ratio: float = ALPHA_HEDGE_RATIO
    v2_trade_limit: int | None = 200
    v2_position_limit: int | None = 300
    v2_gross_position_penalty: float = 0.1
    v2_solver_time_limit_seconds: float = 0.5
    delta_entry_risk_band: float = 30_000.0
    delta_target_risk_band: float = 10_000.0
    gamma_entry_risk_band: float = 10_000.0
    gamma_target_risk_band: float = 10_000.0
    max_legs: int = 4
    max_contracts_per_batch: int = 200
    depth_fraction: float = 0.50
    minimum_target_gross_reduction: int = 200
    minimum_target_reduction_fraction: float = 0.20
    minimum_slice_gross_reduction: int = 25
    delta_risk_tolerance: float = 1.0
    gamma_risk_tolerance: float = 1.0
    alpha_greek_tolerance: float = 1e-6
    solver_time_limit_seconds: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.option_multiplier, bool) or self.option_multiplier <= 0:
            raise ValueError("option_multiplier must be positive")
        for name in (
            "option_fee", "risk_spot_shock_fraction", "risk_vol_shock",
            "margin_limit_fraction", "alpha_hedge_ratio",
            "v2_gross_position_penalty", "v2_solver_time_limit_seconds",
            "delta_entry_risk_band", "delta_target_risk_band",
            "gamma_entry_risk_band", "gamma_target_risk_band",
            "depth_fraction", "minimum_target_reduction_fraction",
            "delta_risk_tolerance", "gamma_risk_tolerance",
            "alpha_greek_tolerance", "solver_time_limit_seconds",
        ):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.option_fee < 0 or self.risk_spot_shock_fraction <= 0 or self.risk_vol_shock < 0:
            raise ValueError("fees and risk shocks are invalid")
        if not 0 < self.margin_limit_fraction <= 1:
            raise ValueError("margin_limit_fraction must be in (0, 1]")
        if not 0 <= self.alpha_hedge_ratio <= ALPHA_HEDGE_RATIO:
            raise ValueError("alpha_hedge_ratio must be between zero and 30%")
        for name in ("v2_trade_limit", "v2_position_limit"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"{name} must be positive or None")
        if self.v2_solver_time_limit_seconds <= 0 or self.solver_time_limit_seconds <= 0:
            raise ValueError("solver time limits must be positive")
        if not 0 < self.delta_target_risk_band < self.delta_entry_risk_band:
            raise ValueError("Delta target band must be positive and below entry")
        if not 0 < self.gamma_target_risk_band <= self.gamma_entry_risk_band:
            raise ValueError("Gamma target band must be positive and no larger than entry")
        if min(self.max_legs, self.max_contracts_per_batch,
               self.minimum_target_gross_reduction,
               self.minimum_slice_gross_reduction) <= 0:
            raise ValueError("MSH limits must be positive")
        if not 0 < self.depth_fraction <= 1:
            raise ValueError("depth_fraction must be in (0, 1]")
        if not 0 < self.minimum_target_reduction_fraction <= 1:
            raise ValueError("minimum_target_reduction_fraction must be in (0, 1]")
        if min(self.delta_risk_tolerance, self.gamma_risk_tolerance,
               self.alpha_greek_tolerance) < 0:
            raise ValueError("MSH tolerances must be nonnegative")
