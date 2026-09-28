from types import SimpleNamespace
import unittest

from hedge_engine.actions import make_action, scenario_risk
from hedge_engine import Greeks
from hedge_engine.config import HedgeConfig
from hedge_engine.margin import short_option_margin


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
        self.assertEqual(action.lower, -2)  # Selling consumes bid-side depth.

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

    def test_live_margin_formula_for_call_and_put(self) -> None:
        self.assertAlmostEqual(short_option_margin("CALL", 105, 5, 100, 10_000), 120_000)
        self.assertAlmostEqual(short_option_margin("PUT", 100, 5, 100, 10_000), 170_000)


if __name__ == "__main__":
    unittest.main()
