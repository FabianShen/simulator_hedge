import unittest
from datetime import date
from decimal import Decimal

from sim_hedge.execution.alpha.strategy import (
    AlphaPlanError,
    build_short_otm_alpha_plan,
    short_option_opening_margin,
)
from sim_hedge.domain import OptionContract, OptionType


def contract(option_type: OptionType, strike: float, maturity: date) -> OptionContract:
    tag = "C" if option_type is OptionType.CALL else "P"
    return OptionContract(
        instrument=f"{tag}{strike:g}",
        underlying="ETF",
        option_type=option_type,
        strike=strike,
        maturity=maturity,
        contract_multiplier=10_000,
        price_tick=0.0001,
    )


class AlphaStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        near = date(2026, 9, 23)
        later = date(2026, 10, 28)
        self.contracts = [
            contract(option_type, strike, maturity)
            for maturity in (near, later)
            for strike in (80, 90, 100, 110, 120, 130)
            for option_type in (OptionType.CALL, OptionType.PUT)
        ]
        self.prices = {
            item.instrument: Decimal("0.10") for item in self.contracts
        }

    def test_builds_balanced_nearest_expiry_with_one_common_quantity(self) -> None:
        plan = build_short_otm_alpha_plan(
            self.contracts,
            spot=100,
            previous_underlying_close=100,
            previous_settlements=self.prices,
            initial_cash=Decimal("1000000"),
        )

        self.assertEqual(plan.maturity, date(2026, 9, 23))
        self.assertEqual(
            set(plan.target_positions),
            {"C110", "C120", "P90", "P80"},
        )
        self.assertEqual(set(plan.target_positions.values()), {-1})
        self.assertEqual(plan.margin_budget, Decimal("300000.00"))
        self.assertEqual(plan.one_contract_basket_margin, Decimal("263000.00"))

    def test_excludes_atm_and_drops_unpaired_far_otm_contracts(self) -> None:
        plan = build_short_otm_alpha_plan(
            self.contracts,
            spot=100,
            previous_underlying_close=100,
            previous_settlements=self.prices,
            initial_cash="1000000",
        )

        self.assertNotIn("C100", plan.target_positions)
        self.assertNotIn("P100", plan.target_positions)
        self.assertNotIn("C130", plan.target_positions)

    def test_rejects_missing_prices_and_too_small_budget(self) -> None:
        with self.assertRaisesRegex(AlphaPlanError, "missing reference price"):
            build_short_otm_alpha_plan(
                self.contracts,
                spot=100,
                previous_underlying_close=100,
                previous_settlements={},
                initial_cash="1000000",
            )
        with self.assertRaisesRegex(AlphaPlanError, "cannot fund one"):
            build_short_otm_alpha_plan(
                self.contracts,
                spot=100,
                previous_underlying_close=100,
                previous_settlements=self.prices,
                initial_cash="1000",
            )

    def test_explicit_contract_cap_limits_the_target_not_the_capacity(self) -> None:
        plan = build_short_otm_alpha_plan(
            self.contracts,
            spot=100,
            previous_underlying_close=100,
            previous_settlements=self.prices,
            initial_cash="2000000",
            max_contracts_per_option=1,
        )

        self.assertEqual(plan.margin_capacity, 2)
        self.assertEqual(plan.contracts_per_option, 1)
        self.assertEqual(set(plan.target_positions.values()), {-1})

    def test_exchange_call_and_put_margin_formulas(self) -> None:
        maturity = date(2026, 9, 23)
        call_margin = short_option_opening_margin(
            contract(OptionType.CALL, 110, maturity),
            previous_underlying_close=100,
            previous_settlement="0.10",
        )
        put_margin = short_option_opening_margin(
            contract(OptionType.PUT, 90, maturity),
            previous_underlying_close=100,
            previous_settlement="0.10",
        )

        self.assertEqual(call_margin, Decimal("71000.00"))
        self.assertEqual(put_margin, Decimal("64000.00"))


if __name__ == "__main__":
    unittest.main()
