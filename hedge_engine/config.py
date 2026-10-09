"""Risk and solver settings for the reference hedge policies."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    v2_depth_excess_penalty: float = 20.0
    v2_solver_time_limit_seconds: float = 0.5
    delta_entry_risk_band: float = 30_000.0
    delta_target_risk_band: float = 10_000.0
    gamma_entry_risk_band: float = 10_000.0
    gamma_target_risk_band: float = 10_000.0
    target_delta: float = 0.0
    target_gamma: float = 0.0
    delta_limit: float | None = None
    gamma_limit: float | None = None
    delta_center_risk: float = 0.0
    gamma_center_risk: float = 0.0
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
            "v2_depth_excess_penalty",
            "delta_entry_risk_band", "delta_target_risk_band",
            "gamma_entry_risk_band", "gamma_target_risk_band",
            "target_delta", "target_gamma", "delta_center_risk", "gamma_center_risk",
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
        if self.v2_depth_excess_penalty < 0:
            raise ValueError("v2_depth_excess_penalty must be nonnegative")
        if self.target_delta != 0 and self.delta_limit is None:
            raise ValueError("a nonzero target_delta requires delta_limit")
        if self.target_gamma != 0 and self.gamma_limit is None:
            raise ValueError("a nonzero target_gamma requires gamma_limit")
        for name in ("delta_limit", "gamma_limit"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive when provided")
        if self.delta_limit is None:
            valid_delta_bands = (
                0 < self.delta_target_risk_band < self.delta_entry_risk_band
            )
        else:
            # resolved_for() maps the legacy single delta band to entry=target.
            valid_delta_bands = (
                0 < self.delta_target_risk_band <= self.delta_entry_risk_band
            )
        if not valid_delta_bands:
            raise ValueError("Delta target band must be positive and no larger than entry")
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

    def resolved_for(self, spot: float) -> "HedgeConfig":
        """Convert raw-Greek targets/bands into the engine's scenario-risk units."""
        if not isfinite(spot) or spot <= 0:
            raise ValueError("spot must be finite and positive")
        updates = {}
        spot_move = float(spot) * self.risk_spot_shock_fraction
        if self.delta_limit is not None:
            updates.update(
                delta_entry_risk_band=self.delta_limit * spot_move,
                delta_target_risk_band=self.delta_limit * spot_move,
                delta_center_risk=self.target_delta * spot_move,
            )
        if self.gamma_limit is not None:
            gamma_scale = 0.5 * spot_move**2
            updates.update(
                gamma_entry_risk_band=self.gamma_limit * gamma_scale,
                gamma_target_risk_band=self.gamma_limit * gamma_scale,
                gamma_center_risk=self.target_gamma * gamma_scale,
            )
        return replace(self, **updates) if updates else self
