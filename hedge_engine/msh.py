"""Stateless minimal-sufficient hedge policy with displayed-depth limits."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from numbers import Integral
from typing import Mapping

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .config import HedgeConfig
from .state import ValidatedHedgeState


@dataclass(frozen=True)
class _Candidate:
    code: str
    current: int
    strategy: int
    delta_risk: float
    gamma_risk: float
    vega: float
    theta: float
    margin: float
    friction: float
    buy_depth: int
    sell_depth: int

class MinimalSufficientHedge:
    """Reduce physical hedge inventory without worsening protected exposures."""

    def __init__(self, config: HedgeConfig, capital: float):
        if not isfinite(capital) or capital <= 0:
            raise ValueError("capital must be finite and positive")
        self.config = config
        self.capital = float(capital)

    def propose(self, state: ValidatedHedgeState) -> dict[str, int]:
        """Compute a target and executable migration slice without retained state."""
        if state.delta_breached or state.gamma_breached:
            return {}
        target = self._optimize(state, target_map=None)
        if target is None:
            return {}
        orders = self._optimize(state, target_map=target)
        if orders is None:
            return {}
        quantities = {
            code: int(orders.get(code, 0)) - int(state.context.hedge_positions.get(code, 0))
            for code in set(orders) | set(state.context.hedge_positions)
        }
        quantities = {code: quantity for code, quantity in quantities.items() if quantity}
        result = self._evaluate_state(state, quantities, target_map=target, require_depth=True)
        return quantities if result["safe"] else {}
    def _candidates(self, state: ValidatedHedgeState) -> list[_Candidate]:
        multiplier = self.config.option_multiplier
        spot_move = state.spot * self.config.risk_spot_shock_fraction
        candidates = []
        for code, instrument in sorted(state.instruments.items()):
            if code not in state.hedge_universe:
                continue
            metrics = instrument.metrics
            values = [float(metrics[name]) for name in ("delta", "gamma", "vega", "theta")]
            if (not all(np.isfinite(values)) or not np.isfinite(instrument.margin)
                    or instrument.bid is None or instrument.ask is None
                    or instrument.ask < instrument.bid):
                continue
            candidates.append(
                _Candidate(
                    code=str(code),
                    current=int(state.context.hedge_positions.get(code, 0)),
                    strategy=int(state.context.strategy_positions.get(code, 0)),
                    delta_risk=values[0] * multiplier * spot_move,
                    gamma_risk=0.5 * values[1] * multiplier * spot_move**2,
                    vega=values[2] * multiplier,
                    theta=values[3] * multiplier,
                    margin=float(instrument.margin),
                    friction=(
                        0.5 * (instrument.ask - instrument.bid) * multiplier
                        + self.config.option_fee
                    ),
                    buy_depth=0 if instrument.ask_size is None else int(instrument.ask_size),
                    sell_depth=0 if instrument.bid_size is None else int(instrument.bid_size),
                )
            )
        return candidates

    def _optimize(
        self, state: ValidatedHedgeState, target_map: Mapping[str, int] | None
    ) -> dict[str, int] | None:
        candidates = self._candidates(state)
        if not candidates:
            return None
        current_gross = sum(abs(int(q)) for q in state.context.hedge_positions.values())
        minimum = (
            self.config.minimum_slice_gross_reduction
            if target_map is not None
            else max(
                self.config.minimum_target_gross_reduction,
                int(np.ceil(current_gross * self.config.minimum_target_reduction_fraction)),
            )
        )
        if current_gross < minimum:
            return None

        n = len(candidates)
        # Blocks: final q, |q| x, combined short s, active z,
        # |q-current| u, and changed-leg binary w.
        q0, x0, s0, z0, u0, w0 = (0, n, 2 * n, 3 * n, 4 * n, 5 * n)
        size = 6 * n
        rows: list[np.ndarray] = []
        lower: list[float] = []
        upper: list[float] = []

        def constraint(entries, lb=-np.inf, ub=np.inf):
            row = np.zeros(size)
            for index, value in entries:
                row[index] += value
            rows.append(row)
            lower.append(lb)
            upper.append(ub)

        position_limit = self.config.v2_position_limit
        big_m = max(
            current_gross,
            max(abs(item.current) for item in candidates),
            max(abs(item.strategy) for item in candidates),
            self.config.max_contracts_per_batch,
            1,
        )
        if position_limit is not None:
            big_m = max(big_m, int(position_limit))

        lb = np.zeros(size)
        ub = np.full(size, np.inf)
        lb[q0:x0] = -big_m
        ub[q0:x0] = big_m
        ub[x0:s0] = big_m
        ub[z0:u0] = 1
        ub[w0:] = 1
        integrality = np.zeros(size)
        integrality[q0:x0] = 1
        integrality[z0:u0] = 1
        integrality[w0:] = 1

        for i, item in enumerate(candidates):
            constraint([(x0 + i, 1), (q0 + i, -1)], lb=0)
            constraint([(x0 + i, 1), (q0 + i, 1)], lb=0)
            constraint([(s0 + i, 1), (q0 + i, 1)], lb=-item.strategy)
            constraint([(x0 + i, 1), (z0 + i, -big_m)], ub=0)
            constraint([(u0 + i, 1), (q0 + i, -1)], lb=-item.current)
            constraint([(u0 + i, 1), (q0 + i, 1)], lb=item.current)
            constraint([(u0 + i, 1), (w0 + i, -big_m)], ub=0)
            if position_limit is not None:
                allowed_position = max(position_limit, abs(item.current))
                lb[q0 + i] = max(lb[q0 + i], -allowed_position)
                ub[q0 + i] = min(ub[q0 + i], allowed_position)
            if item.strategy:
                alpha_limit = self.config.alpha_hedge_ratio * abs(item.strategy)
                lb[q0 + i] = max(lb[q0 + i], ceil(-alpha_limit - 1e-12))
                ub[q0 + i] = min(ub[q0 + i], floor(alpha_limit + 1e-12))

            if target_map is not None:
                target = int(target_map.get(item.code, 0))
                low, high = sorted((item.current, target))
                lb[q0 + i], ub[q0 + i] = low, high
                direction_depth = item.buy_depth if target > item.current else item.sell_depth
                depth_cap = int(np.floor(self.config.depth_fraction * direction_depth))
                trade_cap = self.config.max_contracts_per_batch
                if self.config.v2_trade_limit is not None:
                    trade_cap = min(trade_cap, self.config.v2_trade_limit)
                trade_cap = min(trade_cap, depth_cap)
                lb[q0 + i] = max(lb[q0 + i], item.current - trade_cap)
                ub[q0 + i] = min(ub[q0 + i], item.current + trade_cap)

        delta_entries = [(q0 + i, item.delta_risk) for i, item in enumerate(candidates)]
        gamma_entries = [(q0 + i, item.gamma_risk) for i, item in enumerate(candidates)]
        vega_entries = [(q0 + i, item.vega) for i, item in enumerate(candidates)]
        theta_entries = [(q0 + i, item.theta) for i, item in enumerate(candidates)]
        current_delta = sum(item.current * item.delta_risk for item in candidates)
        current_gamma = sum(item.current * item.gamma_risk for item in candidates)
        current_vega = sum(item.current * item.vega for item in candidates)
        current_theta = sum(item.current * item.theta for item in candidates)
        delta_constant = state.delta_risk - current_delta
        gamma_constant = state.gamma_risk - current_gamma
        vega_constant = state.hedge_greeks.vega - current_vega
        theta_constant = state.hedge_greeks.theta - current_theta
        constraint(
            delta_entries,
            -abs(state.delta_risk) - self.config.delta_risk_tolerance - delta_constant,
            abs(state.delta_risk) + self.config.delta_risk_tolerance - delta_constant,
        )
        constraint(
            gamma_entries,
            -abs(state.gamma_risk) - self.config.gamma_risk_tolerance - gamma_constant,
            abs(state.gamma_risk) + self.config.gamma_risk_tolerance - gamma_constant,
        )
        constraint(
            vega_entries,
            -abs(state.hedge_greeks.vega) - self.config.alpha_greek_tolerance - vega_constant,
            abs(state.hedge_greeks.vega) + self.config.alpha_greek_tolerance - vega_constant,
        )
        constraint(
            theta_entries,
            -abs(state.hedge_greeks.theta) - self.config.alpha_greek_tolerance - theta_constant,
            abs(state.hedge_greeks.theta) + self.config.alpha_greek_tolerance - theta_constant,
        )

        current_margin = self._margin(state, state.context.hedge_positions)
        configured_margin = self.capital * self.config.margin_limit_fraction
        margin_cap = min(current_margin, configured_margin)
        fixed_margin = self._fixed_margin(state, {item.code for item in candidates})
        constraint(
            [(s0 + i, item.margin) for i, item in enumerate(candidates)],
            ub=margin_cap - fixed_margin + 1e-6,
        )
        candidate_codes = {item.code for item in candidates}
        fixed_gross = sum(
            abs(int(quantity))
            for code, quantity in state.context.hedge_positions.items()
            if str(code) not in candidate_codes
        )
        constraint(
            [(x0 + i, 1) for i in range(n)],
            ub=current_gross - minimum - fixed_gross,
        )
        if target_map is not None:
            constraint(
                [(u0 + i, 1) for i in range(n)],
                ub=self.config.max_contracts_per_batch,
            )
            constraint(
                [(w0 + i, 1) for i in range(n)],
                ub=self.config.max_legs,
            )

        base_constraint = LinearConstraint(np.asarray(rows), np.asarray(lower), np.asarray(upper))
        constraints: list[LinearConstraint] = [base_constraint]
        objectives = []
        gross = np.zeros(size)
        gross[x0:s0] = 1
        objectives.append(gross)
        margin = np.zeros(size)
        margin[s0:z0] = [item.margin for item in candidates]
        objectives.append(margin)
        active = np.zeros(size)
        active[z0:u0] = 1
        objectives.append(active)
        friction = np.zeros(size)
        friction[u0:w0] = [item.friction for item in candidates]
        objectives.append(friction)

        solution = None
        for objective in objectives:
            result = milp(
                objective,
                integrality=integrality,
                bounds=Bounds(lb, ub),
                constraints=constraints,
                options={
                    "time_limit": self.config.solver_time_limit_seconds,
                    "mip_rel_gap": 0.0,
                    "presolve": True,
                },
            )
            if not result.success or result.x is None:
                return None
            solution = result.x
            optimum = float(objective @ solution)
            tolerance = max(1e-7, abs(optimum) * 1e-9)
            constraints.append(
                LinearConstraint(objective, optimum - tolerance, optimum + tolerance)
            )
        assert solution is not None
        optimized = {
            item.code: int(np.rint(solution[q0 + i]))
            for i, item in enumerate(candidates)
        }
        # Codes absent from the fresh tradable universe remain frozen.
        for code, quantity in state.context.hedge_positions.items():
            optimized.setdefault(str(code), int(quantity))
        return {code: quantity for code, quantity in optimized.items() if quantity}

    def _evaluate_state(
        self,
        state: ValidatedHedgeState,
        orders: Mapping[str, int],
        *,
        target_map: Mapping[str, int] | None,
        require_depth: bool,
    ) -> dict:
        if state.delta_breached or state.gamma_breached:
            return self._result(False, "risk_policy_active")
        if not orders:
            return self._result(False, "empty")
        quantities: dict[str, int] = {}
        for raw_code, raw_quantity in orders.items():
            if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, Integral):
                return self._result(False, "non_integer")
            code, quantity = str(raw_code), int(raw_quantity)
            if quantity:
                quantities[code] = quantities.get(code, 0) + quantity
        if not quantities:
            return self._result(False, "empty")
        if len(quantities) > self.config.max_legs:
            return self._result(False, "max_legs")
        if sum(abs(quantity) for quantity in quantities.values()) > self.config.max_contracts_per_batch:
            return self._result(False, "max_contracts")

        candidates = {item.code: item for item in self._candidates(state)}
        after = dict(state.context.hedge_positions)
        for code, quantity in quantities.items():
            item = candidates.get(code)
            if item is None:
                return self._result(False, "untradable", code=code)
            if self.config.v2_trade_limit is not None and abs(quantity) > self.config.v2_trade_limit:
                return self._result(False, "trade_limit", code=code)
            if require_depth:
                displayed = item.buy_depth if quantity > 0 else item.sell_depth
                if abs(quantity) > int(np.floor(self.config.depth_fraction * displayed)):
                    return self._result(False, "depth", code=code)
            new_quantity = int(after.get(code, 0)) + quantity
            if self.config.v2_position_limit is not None and abs(new_quantity) > max(
                self.config.v2_position_limit, abs(item.current)
            ):
                return self._result(False, "position_limit", code=code)
            if target_map is not None:
                current = int(state.context.hedge_positions.get(code, 0))
                target = int(target_map.get(code, 0))
                if not min(current, target) <= new_quantity <= max(current, target):
                    return self._result(False, "target_direction", code=code)
            alpha = int(state.context.strategy_positions.get(code, 0))
            if alpha and abs(new_quantity) > self.config.alpha_hedge_ratio * abs(alpha) + 1e-9:
                return self._result(False, "alpha_hedge_limit", code=code)
            if new_quantity:
                after[code] = new_quantity
            else:
                after.pop(code, None)

        before_gross = sum(abs(int(q)) for q in state.context.hedge_positions.values())
        after_gross = sum(abs(int(q)) for q in after.values())
        if after_gross > before_gross - self.config.minimum_slice_gross_reduction:
            return self._result(False, "gross_reduction", gross_before=before_gross, gross_after=after_gross)

        delta_after = state.delta_risk
        gamma_after = state.gamma_risk
        vega_after = state.hedge_greeks.vega
        theta_after = state.hedge_greeks.theta
        for code, quantity in quantities.items():
            item = candidates[code]
            delta_after += quantity * item.delta_risk
            gamma_after += quantity * item.gamma_risk
            vega_after += quantity * item.vega
            theta_after += quantity * item.theta
        if abs(delta_after) > abs(state.delta_risk) + self.config.delta_risk_tolerance:
            return self._result(False, "delta", before=state.delta_risk, after=delta_after)
        if abs(gamma_after) > abs(state.gamma_risk) + self.config.gamma_risk_tolerance:
            return self._result(False, "gamma", before=state.gamma_risk, after=gamma_after)
        if abs(vega_after) > abs(state.hedge_greeks.vega) + self.config.alpha_greek_tolerance:
            return self._result(False, "vega", before=state.hedge_greeks.vega, after=vega_after)
        if abs(theta_after) > abs(state.hedge_greeks.theta) + self.config.alpha_greek_tolerance:
            return self._result(False, "theta", before=state.hedge_greeks.theta, after=theta_after)

        margin_before = self._margin(state, state.context.hedge_positions)
        margin_after = self._margin(state, after)
        if margin_after > margin_before + 1e-6:
            return self._result(False, "margin_increase", before=margin_before, after=margin_after)
        if margin_after > self.capital * self.config.margin_limit_fraction + 1e-6:
            return self._result(False, "margin_limit", after=margin_after)
        return self._result(
            True,
            "safe",
            gross_before=before_gross,
            gross_after=after_gross,
            margin_before=margin_before,
            margin_after=margin_after,
            delta_before=state.delta_risk,
            delta_after=delta_after,
            gamma_before=state.gamma_risk,
            gamma_after=gamma_after,
        )

    @staticmethod
    def _result(safe: bool, reason: str, **details) -> dict:
        return {"safe": safe, "reason": reason, **details}

    @staticmethod
    def _fixed_margin(state: ValidatedHedgeState, optimized_codes: set[str]) -> float:
        total = 0.0
        codes = set(state.context.strategy_positions) | set(state.context.hedge_positions)
        for raw_code in codes - optimized_codes:
            code = str(raw_code)
            quantity = int(state.context.strategy_positions.get(code, 0)) + int(
                state.context.hedge_positions.get(code, 0)
            )
            if quantity < 0:
                margin = state.margins.get(code)
                if margin is None or not np.isfinite(margin):
                    return np.inf
                total += -quantity * float(margin)
        return total

    @staticmethod
    def _margin(state: ValidatedHedgeState, hedge_positions: Mapping[str, int]) -> float:
        total = 0.0
        codes = set(state.context.strategy_positions) | set(hedge_positions)
        for raw_code in codes:
            code = str(raw_code)
            quantity = int(state.context.strategy_positions.get(code, 0)) + int(
                hedge_positions.get(code, 0)
            )
            if quantity < 0:
                margin = state.margins.get(code)
                if margin is None or not np.isfinite(margin):
                    return np.inf
                total += -quantity * float(margin)
        return float(total)
