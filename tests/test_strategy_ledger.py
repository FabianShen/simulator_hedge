from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import ConfirmedFill, apply_confirmed_fills, empty_ledger
from sim_hedge.strategy_ledger import ledger_from_payload, ledger_to_payload


class StrategyLedgerSerializationTests(unittest.TestCase):
    def test_round_trips_confirmed_positions_and_trade_identity(self) -> None:
        fill = ConfirmedFill(
            trade_id="T1",
            order_id="O1",
            account_id="A1",
            strategy="ALPHA",
            instrument="OPTION",
            quantity=-1,
            price=Decimal("0.1234"),
            executed_at=datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc),
        )
        original = apply_confirmed_fills(empty_ledger("A1"), (fill,))

        restored = ledger_from_payload(ledger_to_payload(original))

        self.assertEqual(restored, original)
        self.assertEqual(ledger_to_payload(restored)["status"], "CONFIRMED")

    def test_rejects_positions_not_supported_by_applied_trades(self) -> None:
        payload = {
            "status": "CONFIRMED",
            "account_id": "A1",
            "revision": 1,
            "strategies": {
                "ALPHA": {"actual_positions": {"OPTION": -1}},
                "BETA": {"actual_positions": {}},
            },
            "applied_trades": {},
        }

        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            ledger_from_payload(payload)

    def test_round_trips_overlapping_alpha_and_beta_books(self) -> None:
        alpha = ConfirmedFill(
            "T1", "O1", "A1", "ALPHA", "OPTION", -10,
            Decimal("0.1"), datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        beta = ConfirmedFill(
            "T2", "O2", "A1", "BETA", "OPTION", 3,
            Decimal("0.1"), datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        original = apply_confirmed_fills(empty_ledger("A1"), (alpha, beta))

        restored = ledger_from_payload(ledger_to_payload(original))

        self.assertEqual(restored.alpha_positions, {"OPTION": -10})
        self.assertEqual(restored.beta_positions, {"OPTION": 3})


if __name__ == "__main__":
    unittest.main()
