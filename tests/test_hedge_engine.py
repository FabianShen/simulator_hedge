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
            hedge_pair=("CALL", "PUT"),
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
            hedge_pair=("CALL", "PUT"),
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
                hedge_pair=("ONE", "TWO"),
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
            hedge_pair=("CALL", "PUT"),
        )

        tradable = integerize_delta_gamma_hedge(
            continuous,
            instrument_greeks=greeks,
            hedge_pair=("CALL", "PUT"),
        )

        self.assertEqual(tradable.integer_incremental_trades, {"CALL": 3, "PUT": 0})
        self.assertAlmostEqual(tradable.after_integer_hedge.delta, 0.8)
        self.assertAlmostEqual(tradable.after_integer_hedge.gamma, 0)
        self.assertGreater(tradable.normalized_residual, 0)


if __name__ == "__main__":
    unittest.main()
