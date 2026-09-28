import unittest

from hedge_engine import Greeks, InstrumentGreeks, aggregate_greeks


class GreekValueTests(unittest.TestCase):
    def test_greeks_support_addition_and_scaling(self) -> None:
        value = Greeks(delta=2, gamma=3, vega=4, theta=5)
        self.assertEqual(value.scaled(-2), Greeks(-4, -6, -8, -10))
        self.assertEqual(value.plus(value.scaled(-1)), Greeks())

    def test_aggregates_signed_positions_across_instruments(self) -> None:
        instruments = {
            "CALL": InstrumentGreeks("CALL", Greeks(1, 2, 3, 4)),
            "PUT": InstrumentGreeks("PUT", Greeks(-1, 2, -3, -4)),
        }
        self.assertEqual(
            aggregate_greeks({"CALL": 2, "PUT": -1}, instruments),
            Greeks(delta=3, gamma=2, vega=9, theta=12),
        )

    def test_missing_instrument_greeks_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing Greeks for MISSING"):
            aggregate_greeks({"MISSING": 1}, {})


if __name__ == "__main__":
    unittest.main()
