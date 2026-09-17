import unittest
from datetime import date

from sim_hedge.domain import OptionContract, OptionType
from sim_hedge.option_chain import summarize


def contract(option_type: OptionType, strike: float, maturity: date) -> OptionContract:
    return OptionContract(
        instrument=f"{option_type.value}-{strike}-{maturity}",
        underlying="159915.XSHE",
        option_type=option_type,
        strike=strike,
        maturity=maturity,
        contract_multiplier=10000,
        price_tick=0.0001,
    )


class SummarizeOptionChainTests(unittest.TestCase):
    def test_groups_contracts_by_maturity(self) -> None:
        near = date(2026, 9, 23)
        far = date(2026, 10, 28)
        contracts = [
            contract(OptionType.PUT, 3.4, near),
            contract(OptionType.CALL, 3.2, near),
            contract(OptionType.CALL, 3.4, near),
            contract(OptionType.PUT, 3.0, far),
        ]

        summaries = summarize(contracts)

        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].maturity, near)
        self.assertEqual(summaries[0].calls, 2)
        self.assertEqual(summaries[0].puts, 1)
        self.assertEqual(summaries[0].minimum_strike, 3.2)
        self.assertEqual(summaries[0].maximum_strike, 3.4)


if __name__ == "__main__":
    unittest.main()
