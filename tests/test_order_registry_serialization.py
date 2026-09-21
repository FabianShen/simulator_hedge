from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    OrderIntent,
    abandon_unsubmitted_intents,
    bind_broker_order,
    empty_order_registry,
    register_order_intent,
    supersede_order_intent,
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

    def test_round_trips_supersession_audit(self) -> None:
        original_intent = OrderIntent(
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
        registry = register_order_intent(
            empty_order_registry("A1"), original_intent
        )
        registry = bind_broker_order(
            registry, client_order_id="alpha-001", order_id="O-1"
        )
        replacement = replace(
            original_intent,
            client_order_id="manual-001",
            limit_price=Decimal("0.1200"),
        )
        registry = register_order_intent(registry, replacement)
        registry = bind_broker_order(
            registry, client_order_id="manual-001", order_id="O-2"
        )
        registry = supersede_order_intent(
            registry,
            original_client_order_id="alpha-001",
            replacement_client_order_id="manual-001",
        )

        restored = registry_from_payload(registry_to_payload(registry))

        self.assertEqual(restored, registry)

    def test_round_trips_abandoned_unsubmitted_intent(self) -> None:
        value = OrderIntent(
            client_order_id="beta-old",
            account_id="A1",
            strategy="BETA",
            exchange_id="SZSE",
            instrument="OPTION",
            quantity=1,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=Decimal("0.1234"),
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        registry = register_order_intent(empty_order_registry("A1"), value)
        registry = abandon_unsubmitted_intents(registry, ("beta-old",))

        restored = registry_from_payload(registry_to_payload(registry))

        self.assertEqual(restored, registry)

    def test_round_trips_beta_proposal_ownership(self) -> None:
        value = OrderIntent(
            client_order_id="beta-new",
            account_id="A1",
            strategy="BETA",
            exchange_id="SZSE",
            instrument="OPTION",
            quantity=1,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=Decimal("0.1234"),
            created_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            proposal_id="hedge-proposal-1",
        )
        registry = register_order_intent(empty_order_registry("A1"), value)

        restored = registry_from_payload(registry_to_payload(registry))

        self.assertEqual(restored, registry)
        self.assertEqual(
            restored.intents["beta-new"].proposal_id, "hedge-proposal-1"
        )

if __name__ == "__main__":
    unittest.main()
