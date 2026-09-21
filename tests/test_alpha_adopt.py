from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    bind_broker_order,
    empty_ledger,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.adapters.sim_trading import BrokerOrder, ConfirmedTradePage
from sim_hedge.alpha_adopt import adopt_alpha_replacement


NOW = datetime(2026, 9, 21, 1, 35, tzinfo=timezone.utc)


def order(
    *,
    order_id: str,
    client_order_id: str,
    status: str,
    traded: int,
    cancelled: int,
    price: str,
) -> BrokerOrder:
    return BrokerOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        account_id="A1",
        exchange_id="SZSE",
        instrument="90007076",
        direction="SELL",
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal(price),
        total_volume=1,
        traded_volume=traded,
        remaining_volume=0,
        cancelled_volume=cancelled,
        status=status,
        created_at=NOW,
    )


class FakeSource:
    def __init__(self, original: BrokerOrder, replacement: BrokerOrder) -> None:
        self.orders = {
            original.order_id: original,
            replacement.order_id: replacement,
        }
        self.fill = ConfirmedFill(
            trade_id="TR",
            order_id=replacement.order_id,
            account_id="A1",
            strategy="ALPHA",
            instrument=replacement.instrument,
            quantity=-1,
            price=replacement.limit_price or Decimal("0.0025"),
            executed_at=NOW,
        )

    def load_order(self, order_id, account_id):
        self.account_id = account_id
        return self.orders[order_id]

    def load_confirmed_trade_page(self, account_id, ownership, *, cursor, limit):
        if ownership.get(self.fill.order_id) != "ALPHA":
            raise AssertionError("replacement ownership was not registered")
        return ConfirmedTradePage((self.fill,), None, False)


class AlphaAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        intent = OrderIntent(
            client_order_id="alpha-original",
            account_id="A1",
            strategy="ALPHA",
            exchange_id="SZSE",
            instrument="90007076",
            quantity=-1,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=Decimal("0.0030"),
            created_at=NOW,
        )
        self.registry = register_order_intent(empty_order_registry("A1"), intent)
        self.registry = bind_broker_order(
            self.registry,
            client_order_id="alpha-original",
            order_id="ORIGINAL",
        )
        self.original = order(
            order_id="ORIGINAL",
            client_order_id="alpha-original",
            status="CANCELLED",
            traded=0,
            cancelled=1,
            price="0.0030",
        )
        self.replacement = order(
            order_id="REPLACEMENT",
            client_order_id="manual-replacement",
            status="FILLED",
            traded=1,
            cancelled=0,
            price="0.0025",
        )

    def test_adopts_exact_manual_replacement_and_preserves_audit(self) -> None:
        registry, ledger, report = adopt_alpha_replacement(
            source=FakeSource(self.original, self.replacement),
            registry=self.registry,
            ledger=empty_ledger("A1"),
            original_client_order_id="alpha-original",
            replacement_order_id="REPLACEMENT",
        )

        self.assertEqual(
            registry.superseded_client_order_ids,
            {"alpha-original": "manual-replacement"},
        )
        self.assertEqual(registry.broker_orders["REPLACEMENT"], "manual-replacement")
        self.assertEqual(ledger.alpha_positions, {"90007076": -1})
        self.assertEqual(report["status"], "ADOPTED")

    def test_rejects_replacement_for_a_different_instrument(self) -> None:
        replacement = replace(self.replacement, instrument="OTHER")

        with self.assertRaisesRegex(ValueError, "does not match"):
            adopt_alpha_replacement(
                source=FakeSource(self.original, replacement),
                registry=self.registry,
                ledger=empty_ledger("A1"),
                original_client_order_id="alpha-original",
                replacement_order_id="REPLACEMENT",
            )

    def test_rejects_original_order_that_has_a_fill(self) -> None:
        original = replace(
            self.original,
            status="PARTIALLY_CANCELLED",
            traded_volume=1,
            cancelled_volume=0,
        )

        with self.assertRaisesRegex(ValueError, "cancelled with zero fills"):
            adopt_alpha_replacement(
                source=FakeSource(original, self.replacement),
                registry=self.registry,
                ledger=empty_ledger("A1"),
                original_client_order_id="alpha-original",
                replacement_order_id="REPLACEMENT",
            )


if __name__ == "__main__":
    unittest.main()
