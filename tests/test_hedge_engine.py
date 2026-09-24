import unittest

from hedge_engine import (
    Greeks,
    InstrumentGreeks,
    evaluate_delta_gamma_hedge,
    integerize_delta_gamma_hedge,
)


class DeltaGammaHedgeEngineTests(unittest.TestCase):
    def test_returns_incremental_trades_that_neutralize_current_risk(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }

        decision = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -1},
            hedge_positions={},
            instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT"),
        )

        self.assertAlmostEqual(decision.incremental_trades["CALL"], 3)
        self.assertAlmostEqual(decision.incremental_trades["PUT"], 1)
        self.assertAlmostEqual(decision.after_hedge.delta, 0)
        self.assertAlmostEqual(decision.after_hedge.gamma, 0)

    def test_existing_hedge_positions_are_included_before_solving_increment(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }

        decision = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -1},
            hedge_positions={"CALL": 2},
            instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT"),
        )

        self.assertAlmostEqual(decision.incremental_trades["CALL"], 1)
        self.assertAlmostEqual(decision.incremental_trades["PUT"], 1)
        self.assertAlmostEqual(decision.after_hedge.delta, 0)
        self.assertAlmostEqual(decision.after_hedge.gamma, 0)

    def test_rejects_a_singular_hedge_pair(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "ONE": InstrumentGreeks("ONE", Greeks(delta=1, gamma=1)),
            "TWO": InstrumentGreeks("TWO", Greeks(delta=2, gamma=2)),
        }

        with self.assertRaisesRegex(ValueError, "singular"):
            evaluate_delta_gamma_hedge(
                alpha_positions={"ALPHA": -1},
                hedge_positions={},
                instrument_greeks=greeks,
                hedge_universe=("ONE", "TWO"),
            )

    def test_selects_best_integer_combination_around_continuous_solution(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2.2, gamma=3)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }
        continuous = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -1},
            hedge_positions={},
            instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT"),
        )

        tradable = integerize_delta_gamma_hedge(
            continuous,
            instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT"),
            alpha_positions={"ALPHA": -1},
            hedge_positions={},
        )

        self.assertEqual(tradable.integer_incremental_trades, {"CALL": 3, "PUT": 0})
        self.assertAlmostEqual(tradable.after_integer_hedge.delta, 0.8)
        self.assertAlmostEqual(tradable.after_integer_hedge.gamma, 0)
        self.assertGreater(tradable.normalized_residual, 0)

    def test_uses_more_than_two_legs_to_neutralize_risk(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
            "WING": InstrumentGreeks("WING", Greeks(delta=0.5, gamma=0.8)),
        }
        decision = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -10}, hedge_positions={},
            instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT", "WING"),
        )

        self.assertEqual(set(decision.incremental_trades), {"CALL", "PUT", "WING"})
        self.assertTrue(all(abs(value) > 1e-9 for value in decision.incremental_trades.values()))
        self.assertAlmostEqual(decision.after_hedge.delta, 0)
        self.assertAlmostEqual(decision.after_hedge.gamma, 0)

    def test_clamps_each_alpha_beta_target_to_thirty_percent(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }
        decision = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -10}, hedge_positions={"ALPHA": 1},
            instrument_greeks=greeks,
            hedge_universe=("ALPHA", "CALL", "PUT"),
        )
        tradable = integerize_delta_gamma_hedge(
            decision, instrument_greeks=greeks,
            hedge_universe=("ALPHA", "CALL", "PUT"),
            alpha_positions={"ALPHA": -10}, hedge_positions={"ALPHA": 1},
        )

        self.assertLessEqual(abs(1 + decision.incremental_trades["ALPHA"]), 3)
        self.assertLessEqual(abs(1 + tradable.integer_incremental_trades["ALPHA"]), 3)

    def test_alpha_penalty_prefers_an_equivalent_free_leg(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=1, gamma=2)),
            "FREE": InstrumentGreeks("FREE", Greeks(delta=1, gamma=2)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }
        unpenalized = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -100}, hedge_positions={},
            instrument_greeks=greeks,
            hedge_universe=("ALPHA", "FREE", "PUT"),
            alpha_modification_penalty=0,
        )
        penalized = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -100}, hedge_positions={},
            instrument_greeks=greeks,
            hedge_universe=("ALPHA", "FREE", "PUT"),
            alpha_modification_penalty=20,
        )

        self.assertLess(
            abs(penalized.incremental_trades["ALPHA"]),
            abs(unpenalized.incremental_trades["ALPHA"]),
        )
        self.assertAlmostEqual(penalized.after_hedge.delta, 0)
        self.assertAlmostEqual(penalized.after_hedge.gamma, 0)

    def test_continuous_and_integer_hedge_reach_nonzero_targets(self) -> None:
        greeks = {
            "ALPHA": InstrumentGreeks("ALPHA", Greeks(delta=2, gamma=4)),
            "CALL": InstrumentGreeks("CALL", Greeks(delta=1, gamma=1)),
            "PUT": InstrumentGreeks("PUT", Greeks(delta=-1, gamma=1)),
        }
        decision = evaluate_delta_gamma_hedge(
            alpha_positions={"ALPHA": -1}, hedge_positions={},
            instrument_greeks=greeks, hedge_universe=("CALL", "PUT"),
            target_delta=4, target_gamma=2,
        )
        tradable = integerize_delta_gamma_hedge(
            decision, instrument_greeks=greeks,
            hedge_universe=("CALL", "PUT"),
            alpha_positions={"ALPHA": -1}, hedge_positions={},
            target_delta=4, target_gamma=2,
        )

        self.assertAlmostEqual(decision.after_hedge.delta, 4)
        self.assertAlmostEqual(decision.after_hedge.gamma, 2)
        self.assertAlmostEqual(tradable.after_integer_hedge.delta, 4)
        self.assertAlmostEqual(tradable.after_integer_hedge.gamma, 2)
        self.assertAlmostEqual(tradable.normalized_residual, 0)


if __name__ == "__main__":
    unittest.main()
