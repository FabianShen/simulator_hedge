from types import SimpleNamespace
import unittest

from hedge_engine.actions import gamma_solution_is_useful, make_action, post_trade_feasible, scenario_risk
from hedge_engine import Greeks
from hedge_engine.config import HedgeConfig
from hedge_engine.margin import short_option_margin
from hedge_engine.optimization import HedgeAction, _solve, _solve_best_feasible_gamma


class HedgeActionTests(unittest.TestCase):
    def test_scenario_risk_uses_spot_and_volatility_shocks(self) -> None:
        risk = scenario_risk(Greeks(2, 4, 6, 8), 100, 0.01, 0.02)
        self.assertEqual(tuple(risk), (2.0, 2.0, 0.12))

    def test_action_capacity_uses_side_specific_depth_and_alpha_bound(self) -> None:
        instrument = SimpleNamespace(
            metrics={"delta": 1, "gamma": 1, "vega": 0, "theta": 0},
            bid=1.0, ask=1.1, mid=1.05, bid_size=2, ask_size=5,
        )
        state = SimpleNamespace(
            spot=100,
            instruments={"ALPHA": instrument},
            context=SimpleNamespace(
                strategy_positions={"ALPHA": -10},
                hedge_positions={"ALPHA": 0},
            ),
        )
        action = make_action(
            state, "DG:ALPHA", {"ALPHA": 1}, "DG_HEDGE",
            HedgeConfig(option_multiplier=1), 200,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.upper, 3)  # Alpha beta may reach, but not exceed, 30%.
        self.assertEqual(action.lower, -3)  # Alpha bound stays hard in both directions.
        self.assertEqual((action.depth_sell, action.depth_buy), (2, 5))
        state.context.strategy_positions = {}
        action = make_action(
            state, "DG:ALPHA", {"ALPHA": 1}, "DG_HEDGE",
            HedgeConfig(option_multiplier=1), 200,
        )
        self.assertEqual((action.lower, action.upper), (-200, 200))
        self.assertEqual((action.depth_sell, action.depth_buy), (2, 5))

    def test_missing_displayed_size_uses_configured_trade_cap(self) -> None:
        instrument = SimpleNamespace(
            metrics={"delta": 1, "gamma": 1, "vega": 0, "theta": 0},
            bid=1.0, ask=1.0, mid=1.0, bid_size=None, ask_size=None,
        )
        state = SimpleNamespace(
            spot=100,
            instruments={"C": instrument},
            context=SimpleNamespace(strategy_positions={}, hedge_positions={}),
        )
        action = make_action(
            state, "DG:C", {"C": 1}, "DG_HEDGE", HedgeConfig(), 7
        )
        self.assertEqual((action.lower, action.upper), (-7, 7))
        self.assertEqual((action.depth_sell, action.depth_buy), (7, 7))

    def test_synthetic_depth_uses_both_legs_and_reverses_with_direction(self) -> None:
        call = SimpleNamespace(
            metrics={"delta": 1, "gamma": 1, "vega": 0, "theta": 0},
            bid=1.0, ask=1.0, mid=1.0, bid_size=2, ask_size=7,
        )
        put = SimpleNamespace(
            metrics={"delta": -1, "gamma": 1, "vega": 0, "theta": 0},
            bid=1.0, ask=1.0, mid=1.0, bid_size=5, ask_size=3,
        )
        state = SimpleNamespace(
            spot=100, instruments={"C": call, "P": put},
            context=SimpleNamespace(strategy_positions={}, hedge_positions={}),
        )
        action = make_action(state, "PAIR", {"C": 1, "P": -1}, "DG_HEDGE", HedgeConfig(), 7)
        self.assertEqual((action.lower, action.upper), (-7, 7))
        self.assertEqual((action.depth_sell, action.depth_buy), (2, 5))
        put.bid_size = 0
        action = make_action(state, "PAIR", {"C": 1, "P": -1}, "DG_HEDGE", HedgeConfig(), 7)
        self.assertEqual((action.depth_sell, action.depth_buy), (2, 0))
        self.assertEqual((action.lower, action.upper), (-7, 7))

    def test_multiple_contract_leg_keeps_trade_cap_hard(self) -> None:
        instrument = SimpleNamespace(
            metrics={"delta": 1, "gamma": 1, "vega": 0, "theta": 0},
            bid=1.0, ask=1.0, mid=1.0, bid_size=5, ask_size=9,
        )
        state = SimpleNamespace(
            spot=100, instruments={"C": instrument},
            context=SimpleNamespace(strategy_positions={}, hedge_positions={}),
        )
        action = make_action(state, "PAIR", {"C": -2}, "DG_HEDGE", HedgeConfig(), 7)
        self.assertEqual((action.lower, action.upper), (-3, 3))
        self.assertEqual((action.depth_sell, action.depth_buy), (4, 2))

    def test_usefulness_gate_includes_directional_depth_cost(self) -> None:
        state = SimpleNamespace(
            delta_risk=0.0, gamma_risk=-20.0,
            delta_breached=False, gamma_breached=True,
            context=SimpleNamespace(positions={}, hedge_positions={}), margins={"C": 1},
        )
        action = HedgeAction("C", "DG_HEDGE", {"C": 1}, (0, 10, 0), 2, -10, 10, 3, 1)
        common = dict(capital=1000, margin_limit_fraction=0.7, position_limit=300)
        # Benefit=20, transaction cost=4, buy excess=1. Equality is rejected.
        for penalty, useful in ((0, True), (15, True), (16, False), (20, False)):
            with self.subTest(penalty=penalty):
                self.assertEqual(gamma_solution_is_useful(
                    state, [action], [2], depth_excess_penalty=penalty, **common,
                ), useful)
        state.gamma_risk = 20.0
        # Selling two is within bid depth, irrespective of a large penalty.
        self.assertTrue(gamma_solution_is_useful(
            state, [action], [-2], depth_excess_penalty=1e6, **common,
        ))

    def test_target_solver_penalty_selects_depth_feasible_alternative(self) -> None:
        actions = [
            HedgeAction("CHEAP", "DG_HEDGE", {"A": 1}, (1, 0, 0), 0, -10, 10, 0, 0),
            HedgeAction("DEEP", "DG_HEDGE", {"B": 1}, (1, 0, 0), 5, -10, 10, 10, 10),
        ]
        common = dict(
            actions=actions, current_risk=[-3, 0, 0], target_band=0, breached=(True, False),
            combined_positions={}, hedge_positions={}, margin_per_contract={"A": 1, "B": 1},
            margin_limit=1000, position_limit=300, gross_position_penalty=0.1, time_limit=1,
        )
        self.assertEqual(tuple(_solve(depth_excess_penalty=0, **common)), (3, 0))
        self.assertEqual(tuple(_solve(depth_excess_penalty=1e6, **common)), (0, 3))

    def test_residual_solvers_use_directional_depth_and_keep_safety_limits(self) -> None:
        action = HedgeAction("C", "DG_HEDGE", {"C": 1}, (0, 100, 0), 0, -5, 5, 2, 1)
        common = dict(
            actions=[action], combined_positions={}, hedge_positions={},
            margin_per_contract={"C": 10}, margin_limit=1000,
            position_limit=300, gross_position_penalty=0.1, time_limit=1,
        )
        for gamma, direction, deep in ((1000, -1, 2), (-1000, 1, 1)):
            for penalty, expected in ((0, direction * 5), (1e6, direction * deep)):
                with self.subTest(gamma=gamma, penalty=penalty):
                    self.assertEqual(tuple(_solve_best_feasible_gamma(
                        delta_risk=0, gamma_risk=gamma, depth_excess_penalty=penalty, **common,
                    )), (expected,))
                    self.assertEqual(tuple(_solve(
                        current_risk=[0, gamma, 0], breached=(False, True),
                        depth_excess_penalty=penalty, **common,
                    )), (expected,))
        self.assertEqual(tuple(_solve_best_feasible_gamma(
            delta_risk=0, gamma_risk=1000, depth_excess_penalty=0,
            **{**common, "position_limit": 3},
        )), (-3,))
        self.assertEqual(tuple(_solve_best_feasible_gamma(
            delta_risk=0, gamma_risk=1000, depth_excess_penalty=0,
            **{**common, "margin_limit": 20},
        )), (-2,))

    def test_depth_penalty_config_requires_finite_nonnegative_value(self) -> None:
        self.assertEqual(HedgeConfig().v2_depth_excess_penalty, 20)
        self.assertEqual(HedgeConfig(v2_depth_excess_penalty=0).v2_depth_excess_penalty, 0)
        for penalty in (-1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(penalty=penalty), self.assertRaisesRegex(ValueError, "v2_depth_excess_penalty"):
                HedgeConfig(v2_depth_excess_penalty=penalty)

    def test_nonworsening_is_enforced_for_each_unbreached_dimension(self) -> None:
        for breached, current, cheap, neutral in (
            ((True, False), [-10, -0.5, 0], (1, -0.02, 0), (1, 0, 0)),
            ((False, True), [-0.5, -10, 0], (-0.02, 1, 0), (0, 1, 0)),
        ):
            with self.subTest(breached=breached):
                actions = [
                    HedgeAction("CHEAP", "DG_HEDGE", {"A": 1}, cheap, 0, -20, 20, 20, 20),
                    HedgeAction("NEUTRAL", "DG_HEDGE", {"B": 1}, neutral, 2, -20, 20, 20, 20),
                ]
                common = dict(
                    actions=actions, current_risk=current, combined_positions={}, hedge_positions={},
                    margin_per_contract={"A": 1, "B": 1}, margin_limit=1000, position_limit=300,
                    gross_position_penalty=0.1, time_limit=1, depth_excess_penalty=20, target_band=1,
                )
                self.assertEqual(tuple(_solve(breached=(True, True), **common)), (9, 0))
                self.assertEqual(tuple(_solve(breached=breached, **common)), (0, 9))

    def test_both_solvers_allow_frozen_legacy_inventory_but_cap_new_and_existing_growth(self) -> None:
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                actions = [HedgeAction(code, "DG_HEDGE", {code: 1}, (0, 1, 0),
                                       0, -1000, 1000, 1000, 1000) for code in ("OLD", "NEW", "SMALL")]
                positions = {"OLD": sign * 600, "FROZEN": sign * 700, "SMALL": sign * 290}
                common = dict(
                    actions=actions, combined_positions=positions, hedge_positions=positions,
                    margin_per_contract={code: 1 for code in (*positions, "NEW")}, margin_limit=10000,
                    position_limit=300, gross_position_penalty=0.1, time_limit=1, depth_excess_penalty=0,
                )
                # Risk reduction would add to OLD, so use only the remaining
                # capacity on SMALL and the configured cap on NEW.
                gamma = -sign * 1000
                expected = (0, sign * 300, sign * 10)
                self.assertEqual(tuple(_solve(
                    current_risk=[0, gamma, 0], breached=(False, True), **common,
                )), expected)
                self.assertEqual(tuple(_solve_best_feasible_gamma(
                    delta_risk=0, gamma_risk=gamma, **common,
                )), expected)

    def test_post_trade_check_preserves_legacy_ceiling_for_long_and_short_inventory(self) -> None:
        actions = [HedgeAction(code, "DG_HEDGE", {code: 1}, (0, 0, 0),
                               0, -1000, 1000, 1000, 1000) for code in ("OLD", "NEW")]
        for sign in (-1, 1):
            positions = {"OLD": sign * 600, "FROZEN": sign * 700}
            state = SimpleNamespace(
                context=SimpleNamespace(positions=positions, hedge_positions=positions),
                margins={"OLD": 1, "FROZEN": 1, "NEW": 1},
            )
            for vector, expected in (([0, 0], True), ([-sign, 0], True),
                                     ([sign, 0], False), ([0, sign * 300], True),
                                     ([0, sign * 301], False)):
                with self.subTest(sign=sign, vector=vector):
                    self.assertEqual(post_trade_feasible(
                        state, actions, vector, capital=10000, margin_limit_fraction=0.7, position_limit=300,
                    ), expected)

    def test_live_margin_formula_for_call_and_put(self) -> None:
        self.assertAlmostEqual(short_option_margin("CALL", 105, 5, 100, 10_000), 120_000)
        self.assertAlmostEqual(short_option_margin("PUT", 100, 5, 100, 10_000), 170_000)


if __name__ == "__main__":
    unittest.main()
