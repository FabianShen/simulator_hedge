import unittest
from datetime import date

from sim_hedge.domain import OptionContract, OptionType
from sim_hedge.sim_hedge.strategy_universe import select_strategy_universe


def contract(option_type: OptionType, strike: float, maturity: date) -> OptionContract:
    return OptionContract(
        instrument=f"{maturity}-{strike}-{option_type.value}",
        underlying="159915.XSHE",
        option_type=option_type,
        strike=strike,
        maturity=maturity,
        contract_multiplier=10_000,
        price_tick=0.0001,
    )


def paired_chain(maturity: date, strikes: list[float]) -> list[OptionContract]:
    return [
        contract(option_type, strike, maturity)
        for strike in strikes
        for option_type in (OptionType.CALL, OptionType.PUT)
    ]


class SelectStrategyUniverseTests(unittest.TestCase):
    def test_selects_nearest_expiry_and_strikes_around_spot(self) -> None:
        near = date(2026, 9, 23)
        far = date(2026, 10, 28)
        chain = paired_chain(near, [3.1, 3.2, 3.3, 3.4, 3.5])
        chain += paired_chain(far, [3.2, 3.3, 3.4])

        universe = select_strategy_universe(chain, 3.31, strike_wings=1)

        self.assertEqual(universe.maturity, near)
        self.assertEqual(universe.center_strike, 3.3)
        self.assertEqual(universe.strikes, (3.2, 3.3, 3.4))
        self.assertEqual(len(universe.contracts), 6)
        self.assertEqual(len(universe.instruments), 6)

    def test_can_select_a_later_expiry(self) -> None:
        near = date(2026, 9, 23)
        far = date(2026, 10, 28)
        chain = paired_chain(near, [3.2]) + paired_chain(far, [3.2, 3.3])

        universe = select_strategy_universe(chain, 3.3, expiry_index=1)

        self.assertEqual(universe.maturity, far)

    def test_excludes_a_strike_without_a_complete_pair(self) -> None:
        maturity = date(2026, 9, 23)
        chain = paired_chain(maturity, [3.2])
        chain.append(contract(OptionType.CALL, 3.3, maturity))

        universe = select_strategy_universe(chain, 3.3)

        self.assertEqual(universe.strikes, (3.2,))
        self.assertEqual(len(universe.contracts), 2)

    def test_rejects_an_expiry_outside_the_chain(self) -> None:
        chain = paired_chain(date(2026, 9, 23), [3.2])

        with self.assertRaisesRegex(ValueError, "outside"):
            select_strategy_universe(chain, 3.2, expiry_index=1)


if __name__ == "__main__":
    unittest.main()
