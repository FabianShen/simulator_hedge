"""Integer, execution-cost-aware hedge optimization."""

from dataclasses import dataclass
from typing import Mapping

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


@dataclass(frozen=True)
class HedgeAction:
    name: str
    role: str
    legs: Mapping[str, int]
    risk: tuple[float, float, float]
    cost: float
    lower: int
    upper: int


def _solve(
    actions,
    current_risk,
    combined_positions,
    hedge_positions,
    margin_per_contract,
    margin_limit,
    position_limit,
    gross_position_penalty,
    time_limit,
    target_band=None,
):
    action_count = len(actions)
    hedge_codes = sorted(set(hedge_positions).union(*(action.legs for action in actions)))
    combined_codes = sorted(set(combined_positions).union(hedge_codes))
    hedge_matrix = np.array(
        [[action.legs.get(code, 0) for action in actions] for code in hedge_codes],
        dtype=float,
    )
    combined_matrix = np.array(
        [[action.legs.get(code, 0) for action in actions] for code in combined_codes],
        dtype=float,
    )
    risk_matrix = np.array([action.risk for action in actions], dtype=float).T

    x_slice = slice(0, action_count)
    trade_slice = slice(action_count, 2 * action_count)
    hedge_slice = slice(trade_slice.stop, trade_slice.stop + len(hedge_codes))
    margin_slice = slice(hedge_slice.stop, hedge_slice.stop + len(combined_codes))
    risk_slice = slice(
        margin_slice.stop,
        margin_slice.stop + (0 if target_band is not None else 3),
    )
    size = risk_slice.stop

    objective = np.zeros(size)
    objective[trade_slice] = [action.cost for action in actions]
    objective[hedge_slice] = gross_position_penalty
    if target_band is None:
        objective[risk_slice] = 1.0

    lower = np.zeros(size)
    upper = np.full(size, np.inf)
    lower[x_slice] = [action.lower for action in actions]
    upper[x_slice] = [action.upper for action in actions]
    upper[trade_slice] = [max(abs(action.lower), abs(action.upper)) for action in actions]
    if position_limit is not None:
        upper[hedge_slice] = position_limit
    integrality = np.zeros(size)
    integrality[x_slice] = 1

    rows, lows, highs = [], [], []

    def constrain(coefficients, low=-np.inf, high=np.inf):
        rows.append(np.array(coefficients, copy=True))
        lows.append(low)
        highs.append(high)

    for index in range(action_count):
        row = np.zeros(size)
        row[index], row[trade_slice.start + index] = 1.0, -1.0
        constrain(row, high=0.0)
        row[index] = -1.0
        constrain(row, high=0.0)

    for row_index, code in enumerate(hedge_codes):
        base = float(hedge_positions.get(code, 0))
        row = np.zeros(size)
        row[x_slice] = hedge_matrix[row_index]
        row[hedge_slice.start + row_index] = -1.0
        constrain(row, high=-base)
        row[x_slice] *= -1.0
        constrain(row, high=base)

    for row_index, code in enumerate(combined_codes):
        base = float(combined_positions.get(code, 0))
        row = np.zeros(size)
        row[x_slice] = -combined_matrix[row_index]
        row[margin_slice.start + row_index] = -1.0
        constrain(row, high=base)

    margin_row = np.zeros(size)
    margin_row[margin_slice] = [margin_per_contract[code] for code in combined_codes]
    constrain(margin_row, high=margin_limit)

    if target_band is not None:
        for risk_index in range(3):
            row = np.zeros(size)
            row[x_slice] = risk_matrix[risk_index]
            constrain(
                row,
                low=-target_band - current_risk[risk_index],
                high=target_band - current_risk[risk_index],
            )
    else:
        for risk_index in range(3):
            row = np.zeros(size)
            row[x_slice] = risk_matrix[risk_index]
            row[risk_slice.start + risk_index] = -1.0
            constrain(row, high=-current_risk[risk_index])
            row[x_slice] *= -1.0
            constrain(row, high=current_risk[risk_index])

    result = milp(
        objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(np.vstack(rows), lows, highs),
        options={"time_limit": time_limit, "presolve": True},
    )
    if not result.success or result.x is None:
        return None
    return np.rint(result.x[x_slice]).astype(int)


def _solve_best_feasible_gamma(
    actions,
    delta_risk,
    gamma_risk,
    combined_positions,
    hedge_positions,
    margin_per_contract,
    margin_limit,
    position_limit,
    gross_position_penalty,
    time_limit,
):
    action_count = len(actions)
    if not action_count:
        return None

    hedge_codes = sorted(
        set(hedge_positions).union(
            *(action.legs for action in actions)
        )
    )

    combined_codes = sorted(
        set(combined_positions).union(hedge_codes)
    )

    hedge_matrix = np.array(
        [
            [
                action.legs.get(code, 0)
                for action in actions
            ]
            for code in hedge_codes
        ],
        dtype=float,
    )

    combined_matrix = np.array(
        [
            [
                action.legs.get(code, 0)
                for action in actions
            ]
            for code in combined_codes
        ],
        dtype=float,
    )

    risk_matrix = np.array(
        [action.risk for action in actions],
        dtype=float,
    ).T

    x_slice = slice(0, action_count)
    trade_slice = slice(
        action_count,
        2 * action_count,
    )

    hedge_slice = slice(
        trade_slice.stop,
        trade_slice.stop + len(hedge_codes),
    )

    margin_slice = slice(
        hedge_slice.stop,
        hedge_slice.stop + len(combined_codes),
    )

    gamma_index = margin_slice.stop
    size = gamma_index + 1

    objective = np.zeros(size)

    objective[trade_slice] = [
        action.cost
        for action in actions
    ]

    objective[hedge_slice] = (
        gross_position_penalty
    )

    # Raw Gamma residual, exactly as accepted
    # best-feasible Gamma fallback.
    objective[gamma_index] = 1.0

    lower = np.zeros(size)
    upper = np.full(size, np.inf)

    lower[x_slice] = [
        action.lower
        for action in actions
    ]

    upper[x_slice] = [
        action.upper
        for action in actions
    ]

    upper[trade_slice] = [
        max(
            abs(action.lower),
            abs(action.upper),
        )
        for action in actions
    ]

    if position_limit is not None:
        upper[hedge_slice] = position_limit

    integrality = np.zeros(size)
    integrality[x_slice] = 1

    rows = []
    lows = []
    highs = []

    def constrain(
        coefficients,
        low=-np.inf,
        high=np.inf,
    ):
        rows.append(
            np.array(coefficients, copy=True)
        )
        lows.append(low)
        highs.append(high)

    # |trade quantity|
    for index in range(action_count):
        row = np.zeros(size)
        row[index] = 1.0
        row[trade_slice.start + index] = -1.0
        constrain(row, high=0.0)

        row[index] = -1.0
        constrain(row, high=0.0)

    # |hedge position|
    for row_index, code in enumerate(hedge_codes):
        base = float(
            hedge_positions.get(code, 0)
        )

        row = np.zeros(size)
        row[x_slice] = hedge_matrix[row_index]
        row[hedge_slice.start + row_index] = -1.0
        constrain(row, high=-base)

        row[x_slice] *= -1.0
        constrain(row, high=base)

    # Short-position margin variables
    for row_index, code in enumerate(
        combined_codes
    ):
        base = float(
            combined_positions.get(code, 0)
        )

        row = np.zeros(size)
        row[x_slice] = -combined_matrix[row_index]
        row[margin_slice.start + row_index] = -1.0

        constrain(row, high=base)

    margin_row = np.zeros(size)

    margin_row[margin_slice] = [
        margin_per_contract[code]
        for code in combined_codes
    ]

    constrain(
        margin_row,
        high=margin_limit,
    )

    # Delta may improve or stay unchanged,
    # but may not become worse.
    delta_bound = max(
        abs(float(delta_risk)),
        1e-6,
    )

    delta_row = np.zeros(size)
    delta_row[x_slice] = risk_matrix[0]

    constrain(
        delta_row,
        low=-delta_bound - float(delta_risk),
        high=delta_bound - float(delta_risk),
    )

    # Minimize |Gamma_after|.
    gamma_row = np.zeros(size)
    gamma_row[x_slice] = risk_matrix[1]
    gamma_row[gamma_index] = -1.0

    constrain(
        gamma_row,
        high=-float(gamma_risk),
    )

    gamma_row[x_slice] *= -1.0

    constrain(
        gamma_row,
        high=float(gamma_risk),
    )

    result = milp(
        objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(
            np.vstack(rows),
            lows,
            highs,
        ),
        options={
            "time_limit": time_limit,
            "presolve": True,
        },
    )

    if not result.success or result.x is None:
        return None

    return np.rint(
        result.x[x_slice]
    ).astype(int)

