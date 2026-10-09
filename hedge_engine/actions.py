"""Cost, depth, and margin helpers shared by hedge policies."""

from __future__ import annotations

from math import ceil, floor
import numpy as np

from hedge_engine.engine import Greeks
from .optimization import HedgeAction


def scenario_risk(
    greeks: Greeks,
    spot: float,
    spot_shock_fraction: float,
    vol_shock: float,
) -> np.ndarray:
    spot_move = float(spot) * float(spot_shock_fraction)
    return np.asarray(
        [
            greeks.delta * spot_move,
            0.5 * greeks.gamma * spot_move**2,
            greeks.vega * float(vol_shock),
        ],
        dtype=float,
    )


def make_action(state, name, legs, role, config, trade_limit):
    total = {key: 0.0 for key in ("delta", "gamma", "vega", "theta")}
    positive_depth = []
    negative_depth = []
    cost = 0.0
    fallback = trade_limit if trade_limit is not None else config.v2_position_limit
    if fallback is None:
        return None
    for code, leg in legs.items():
        instrument = state.instruments.get(str(code))
        if instrument is None or instrument.bid is None or instrument.ask is None:
            return None
        for key in total:
            total[key] += int(leg) * float(instrument.metrics[key]) * config.option_multiplier
        buy_size = fallback if instrument.ask_size is None else instrument.ask_size
        sell_size = fallback if instrument.bid_size is None else instrument.bid_size
        positive_depth.append((buy_size if leg > 0 else sell_size) / abs(leg))
        negative_depth.append((sell_size if leg > 0 else buy_size) / abs(leg))
        cost += abs(leg) * (
            max(instrument.ask - instrument.mid, instrument.mid - instrument.bid)
            * config.option_multiplier + config.option_fee
        )
    risk = scenario_risk(
        Greeks(**total), state.spot,
        config.risk_spot_shock_fraction, config.risk_vol_shock,
    )
    depth_sell = int(np.floor(min(negative_depth)))
    depth_buy = int(np.floor(min(positive_depth)))
    # Keep the per-leg trade cap hard, independently of displayed liquidity.
    capacity = int(np.floor(fallback / max(abs(leg) for leg in legs.values())))
    lower, upper = -capacity, capacity
    for code, leg in legs.items():
        alpha = int(state.context.strategy_positions.get(code, 0))
        if not alpha:
            continue
        current = int(state.context.hedge_positions.get(code, 0))
        limit = config.alpha_hedge_ratio * abs(alpha)
        first, second = (-limit - current) / leg, (limit - current) / leg
        lower = max(lower, ceil(min(first, second) - 1e-12))
        upper = min(upper, floor(max(first, second) + 1e-12))
    if lower > upper or (lower == 0 and upper == 0):
        return None
    return HedgeAction(
        name, role, legs, tuple(risk), float(cost), lower, upper,
        depth_sell, depth_buy,
    )


def depth_excess(action: HedgeAction, quantity: int) -> int:
    """Displayed-depth excess in action units, using the executed direction."""
    return max(0, int(quantity) - action.depth_buy, -int(quantity) - action.depth_sell)


def post_trade_feasible(
    state,
    actions,
    vector,
    *,
    capital: float,
    margin_limit_fraction: float,
    position_limit: int | None,
) -> bool:
    combined = dict(state.context.positions)
    hedge = dict(state.context.hedge_positions)
    for action, quantity in zip(actions, vector):
        for code, leg in action.legs.items():
            change = int(quantity) * int(leg)
            combined[code] = int(combined.get(code, 0)) + change
            hedge[code] = int(hedge.get(code, 0)) + change
            if not combined[code]:
                combined.pop(code)
            if not hedge[code]:
                hedge.pop(code)
    if position_limit is not None and any(
        abs(value) > max(position_limit, abs(state.context.hedge_positions.get(code, 0)))
        for code, value in hedge.items()
    ):
        return False
    if any(
        code not in state.margins
        for code, quantity in combined.items()
        if quantity < 0
    ):
        return False
    margin = sum(
        max(-quantity, 0) * state.margins[code]
        for code, quantity in combined.items()
        if quantity < 0
    )
    return margin <= capital * margin_limit_fraction + 1e-9


def gamma_solution_is_useful(
    state, actions, vector, *, capital, margin_limit_fraction, position_limit,
    depth_excess_penalty,
) -> bool:
    """Validate material improvement on the currently breached D/G dimensions."""
    before = (state.delta_risk, state.gamma_risk)
    after = tuple(
        value + sum(float(action.risk[index]) * int(quantity)
                    for action, quantity in zip(actions, vector))
        for index, value in enumerate(before)
    )
    breached = (state.delta_breached, state.gamma_breached)
    benefit = sum(max(abs(old) - abs(new), 0.0)
                  for old, new, active in zip(before, after, breached) if active)
    cost = sum(float(action.cost) * abs(int(quantity))
               + depth_excess_penalty * depth_excess(action, quantity)
               for action, quantity in zip(actions, vector))
    improved = all(not active or abs(new) < abs(old) - 1e-9
                   for old, new, active in zip(before, after, breached))
    nonworsening = all(active or abs(new) <= abs(old) + 1e-6
                       for old, new, active in zip(before, after, breached))
    return bool(
        improved and nonworsening and benefit > cost + 1e-9
        and post_trade_feasible(
            state, actions, vector, capital=capital,
            margin_limit_fraction=margin_limit_fraction,
            position_limit=position_limit,
        )
    )


def orders_from_actions(actions, vector) -> dict[str, int]:
    result: dict[str, int] = {}
    for action, quantity in zip(actions, vector):
        for code, leg in action.legs.items():
            amount = int(quantity) * int(leg)
            if amount:
                result[str(code)] = result.get(str(code), 0) + amount
    return {code: quantity for code, quantity in sorted(result.items()) if quantity}
