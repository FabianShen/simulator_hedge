import unittest

from pricing_engine import OptionPricingResult
from sim_hedge.hedge_plan import _usable_instrument_greeks


def valid(instrument: str) -> OptionPricingResult:
    return OptionPricingResult(
        instrument=instrument,
        status="OK",
        delta=0.5,
        gamma=0.2,
        theta_per_year=-0.1,
        vega_per_absolute_volatility=0.3,
    )


def invalid(instrument: str) -> OptionPricingResult:
    return OptionPricingResult(
        instrument=instrument,
        status="INVALID_INPUT",
        error="market_price is outside no-arbitrage bounds",
    )


class HedgePlanPricingFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "HELD": {"contractMultiplier": 10_000},
            "CANDIDATE": {"contractMultiplier": 10_000},
            "INVALID": {"contractMultiplier": 10_000},
        }

    def test_excludes_invalid_unheld_contract_and_retains_reason(self) -> None:
        greeks, exclusions = _usable_instrument_greeks(
            self.metadata,
            {
                "HELD": valid("HELD"),
                "CANDIDATE": valid("CANDIDATE"),
                "INVALID": invalid("INVALID"),
            },
            {"HELD"},
        )

        self.assertEqual(set(greeks), {"HELD", "CANDIDATE"})
        self.assertEqual(
            exclusions["INVALID"],
            {
                "status": "INVALID_INPUT",
                "reason": "market_price is outside no-arbitrage bounds",
            },
        )

    def test_rejects_invalid_contract_when_it_is_held(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "valid pricing Greeks missing for held positions.*INVALID"
        ):
            _usable_instrument_greeks(
                self.metadata,
                {
                    "HELD": valid("HELD"),
                    "CANDIDATE": valid("CANDIDATE"),
                    "INVALID": invalid("INVALID"),
                },
                {"HELD", "INVALID"},
            )

    def test_missing_pricing_result_is_an_exclusion(self) -> None:
        _, exclusions = _usable_instrument_greeks(
            self.metadata,
            {"HELD": valid("HELD"), "CANDIDATE": valid("CANDIDATE")},
            {"HELD"},
        )

        self.assertEqual(exclusions["INVALID"]["status"], "MISSING")


if __name__ == "__main__":
    unittest.main()
