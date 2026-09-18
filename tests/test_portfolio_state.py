import unittest
from decimal import Decimal

from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot
from sim_hedge.portfolio_state import PortfolioState


class PortfolioStateTests(unittest.TestCase):
    def test_absolute_snapshot_replaces_previous_state(self) -> None:
        state = PortfolioState()
        first = PortfolioSnapshot(
            AccountSnapshot("A", "ETF_OPTION", "ACTIVE"), (), ()
        )
        second = PortfolioSnapshot(
            AccountSnapshot(
                "A", "ETF_OPTION", "ACTIVE", available_cash=Decimal("12.34")
            ),
            (),
            (),
        )

        self.assertEqual(state.replace(first), 1)
        self.assertEqual(state.replace(second), 2)

        self.assertIs(state.snapshot(), second)
        self.assertEqual(state.revision, 2)


if __name__ == "__main__":
    unittest.main()
