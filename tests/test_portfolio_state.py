import unittest
from decimal import Decimal

from sim_hedge.portfolio import (
    AccountSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)
from sim_hedge.state.portfolio_state import PortfolioState


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

    def test_position_events_are_absolute_and_versioned(self) -> None:
        state = PortfolioState()
        initial = PortfolioSnapshot(
            AccountSnapshot("A", "ETF_OPTION", "ACTIVE"),
            (),
            (),
            business_version="10",
        )
        position = PositionSnapshot(
            "P1", "A", "OPTION", "LONG", *(Decimal("1"),) * 5
        )
        state.replace(initial, synchronized=True)

        self.assertTrue(state.upsert_position(position, "11"))
        self.assertFalse(state.upsert_position(position, "11"))
        self.assertTrue(state.remove_position("P1", "12"))

        self.assertEqual(state.snapshot().positions, ())
        self.assertTrue(state.synchronized)
        state.mark_unsynchronized()
        self.assertFalse(state.synchronized)


if __name__ == "__main__":
    unittest.main()
