from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    OrderIntent,
    bind_broker_order,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.order_registry import registry_from_payload, registry_to_payload


class OrderRegistrySerializationTests(unittest.TestCase):
    def test_round_trips_intent_binding_and_strategy_mapping(self) -> None:
        intent = OrderIntent(
            client_order_id="alpha-001",
            account_id="A1",
            strategy="ALPHA",
            exchange_id="SZSE",
            instrument="OPTION",
            quantity=-1,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=Decimal("0.1234"),
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        registry = register_order_intent(empty_order_registry("A1"), intent)
        registry = bind_broker_order(
            registry, client_order_id="alpha-001", order_id="O-1"
        )

        restored = registry_from_payload(registry_to_payload(registry))

        self.assertEqual(restored, registry)
        self.assertEqual(restored.order_strategies, {"O-1": "ALPHA"})


if __name__ == "__main__":
    unittest.main()
