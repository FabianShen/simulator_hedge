"""Cost-, depth-, and margin-aware Delta/Gamma hedge proposals."""

from __future__ import annotations

import numpy as np

from .actions import gamma_solution_is_useful, make_action, orders_from_actions
from .config import HedgeConfig
from .optimization import HedgeAction, _solve, _solve_best_feasible_gamma
from .state import ValidatedHedgeState


def delta_gamma_hedge(
    state: ValidatedHedgeState, config: HedgeConfig, capital: float
) -> dict[str, int]:
    actions = _delta_gamma_actions(state, config)
    if not actions:
        return {}

    delta_scale = max(config.delta_target_risk_band, 1e-12)
    gamma_scale = max(config.gamma_target_risk_band, 1e-12)
    normalized = [
        HedgeAction(
            action.name,
            action.role,
            action.legs,
            (
                float(action.risk[0]) / delta_scale,
                float(action.risk[1]) / gamma_scale,
                0.0,
            ),
            action.cost,
            action.lower,
            action.upper,
        )
        for action in actions
    ]
    common = dict(
        combined_positions=state.context.positions,
        hedge_positions=state.context.hedge_positions,
        margin_per_contract=state.margins,
        margin_limit=capital * config.margin_limit_fraction,
        position_limit=config.v2_position_limit,
        gross_position_penalty=config.v2_gross_position_penalty,
        time_limit=config.v2_solver_time_limit_seconds,
    )
    vector = _solve(
        actions=normalized,
        current_risk=np.asarray(
            [state.delta_risk / delta_scale, state.gamma_risk / gamma_scale, 0.0]
        ),
        target_band=1.0,
        **common,
    )
    if vector is not None and np.any(vector) and gamma_solution_is_useful(
        state,
        actions,
        vector,
        capital=capital,
        margin_limit_fraction=config.margin_limit_fraction,
        position_limit=config.v2_position_limit,
    ):
        return orders_from_actions(actions, vector)

    vector = _solve_best_feasible_gamma(
        actions=actions,
        delta_risk=state.delta_risk,
        gamma_risk=state.gamma_risk,
        **common,
    )
    if vector is None or not np.any(vector) or not gamma_solution_is_useful(
        state,
        actions,
        vector,
        capital=capital,
        margin_limit_fraction=config.margin_limit_fraction,
        position_limit=config.v2_position_limit,
    ):
        return {}
    return orders_from_actions(actions, vector)


def _delta_gamma_actions(
    state: ValidatedHedgeState, config: HedgeConfig
) -> list[HedgeAction]:
    actions: list[HedgeAction] = []
    pair_codes = set(state.atm_pair[1:]) if state.atm_pair else set()
    for code in state.hedge_universe:
        if code in pair_codes:
            continue
        action = make_action(
            state,
            f"DG:{code}",
            {code: 1},
            "DG_HEDGE",
            config,
            config.v2_trade_limit,
        )
        if action is not None:
            actions.append(action)
    if state.atm_pair:
        _, call, put = state.atm_pair
        action = make_action(
            state,
            "DG:CURRENT_ATM_SYNTHETIC",
            {call: 1, put: -1},
            "DG_HEDGE",
            config,
            config.v2_trade_limit,
        )
        if action is not None:
            actions.append(action)
    return actions
