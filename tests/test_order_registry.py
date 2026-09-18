from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    OrderIntent,
    bind_broker_order,
    empty_order_registry,
    register_order_intent,
)


def intent(*, quantity: int = -1) -> OrderIntent:
    return OrderIntent(
        client_order_id="alpha-001",
        account_id="A1",
        strategy="ALPHA",
        instrument="OPTION",
        quantity=quantity,
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("0.1234"),
        created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )


class OrderRegistryTests(unittest.TestCase):
    def test_registers_before_binding_and_exposes_strategy_by_order_id(self) -> None:
        registered = register_order_intent(empty_order_registry("A1"), intent())
        bound = bind_broker_order(
            registered, client_order_id="alpha-001", order_id="O-1"
        )

        self.assertEqual(registered.revision, 1)
        self.assertEqual(bound.revision, 2)
        self.assertEqual(bound.order_strategies, {"O-1": "ALPHA"})

    def test_exact_register_and_bind_replays_are_idempotent(self) -> None:
        registered = register_order_intent(empty_order_registry("A1"), intent())
        self.assertIs(register_order_intent(registered, intent()), registered)
        bound = bind_broker_order(
            registered, client_order_id="alpha-001", order_id="O-1"
        )
        self.assertIs(
            bind_broker_order(bound, client_order_id="alpha-001", order_id="O-1"),
            bound,
        )

    def test_rejects_conflicting_client_order_retry(self) -> None:
        registered = register_order_intent(empty_order_registry("A1"), intent())

        with self.assertRaisesRegex(ValueError, "conflicting"):
            register_order_intent(registered, intent(quantity=-2))

    def test_rejects_binding_before_intent_is_registered(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown client_order_id"):
            bind_broker_order(
                empty_order_registry("A1"),
                client_order_id="alpha-001",
                order_id="O-1",
            )

    def test_rejects_a_second_broker_order_for_one_client_id(self) -> None:
        registered = register_order_intent(empty_order_registry("A1"), intent())
        bound = bind_broker_order(
            registered, client_order_id="alpha-001", order_id="O-1"
        )

        with self.assertRaisesRegex(ValueError, "already bound"):
            bind_broker_order(
                bound, client_order_id="alpha-001", order_id="O-2"
            )


if __name__ == "__main__":
    unittest.main()
