from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import ConfirmedFill, apply_confirmed_fills, empty_ledger


def fill(
    trade_id: str,
    *,
    strategy: str = "ALPHA",
    instrument: str = "OPTION",
    quantity: int = -1,
) -> ConfirmedFill:
    return ConfirmedFill(
        trade_id=trade_id,
        order_id=f"order-{trade_id}",
        account_id="A1",
        strategy=strategy,
        instrument=instrument,
        quantity=quantity,
        price=Decimal("0.1"),
        executed_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )


class StrategyAccountingTests(unittest.TestCase):
    def test_applies_confirmed_alpha_and_beta_fills_to_separate_books(self) -> None:
        ledger = apply_confirmed_fills(
            empty_ledger("A1"),
            (
                fill("T1"),
                fill("T2", strategy="BETA", instrument="HEDGE", quantity=2),
            ),
        )

        self.assertEqual(ledger.alpha_positions, {"OPTION": -1})
        self.assertEqual(ledger.beta_positions, {"HEDGE": 2})
        self.assertEqual(ledger.revision, 1)

    def test_replaying_the_same_trade_is_idempotent(self) -> None:
        original = apply_confirmed_fills(empty_ledger("A1"), (fill("T1"),))
        replayed = apply_confirmed_fills(original, (fill("T1"),))

        self.assertIs(replayed, original)

    def test_rejects_conflicting_contents_for_an_existing_trade_id(self) -> None:
        ledger = apply_confirmed_fills(empty_ledger("A1"), (fill("T1"),))

        with self.assertRaisesRegex(ValueError, "conflicting"):
            apply_confirmed_fills(ledger, (fill("T1", quantity=-2),))

    def test_rejects_cross_book_instrument_ownership(self) -> None:
        ledger = apply_confirmed_fills(empty_ledger("A1"), (fill("T1"),))

        with self.assertRaisesRegex(ValueError, "other strategy"):
            apply_confirmed_fills(
                ledger,
                (fill("T2", strategy="BETA", quantity=1),),
            )

    def test_a_closing_fill_removes_the_position(self) -> None:
        ledger = apply_confirmed_fills(empty_ledger("A1"), (fill("T1"),))
        closed = apply_confirmed_fills(ledger, (fill("T2", quantity=1),))

        self.assertEqual(closed.alpha_positions, {})
        self.assertEqual(closed.revision, 2)


if __name__ == "__main__":
    unittest.main()
