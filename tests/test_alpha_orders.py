import unittest

from sim_hedge.alpha_orders import build_alpha_order_dry_run


def records(quantity: int = -1):
    alpha = {
        "plan_id": "alpha-R1",
        "source_alpha_market_id": "R1",
        "account_id": "A1",
        "as_of": "2026-09-18T02:00:00Z",
        "orders_generated": False,
        "legs": [{"instrument": "9001", "quantity": quantity}],
    }
    return alpha


class AlphaOrderDryRunTests(unittest.TestCase):
    def test_registers_stable_sell_open_limit_intent_without_submission(self) -> None:
        alpha = records()

        output, registry = build_alpha_order_dry_run(
            alpha, exchange_id="SZSE"
        )

        request = output["requests"][0]
        self.assertEqual(request["direction"], "SELL")
        self.assertEqual(request["offset_flag"], "OPEN")
        self.assertEqual(request["order_type"], "COUNTERPARTY")
        self.assertNotIn("limit_price", request)
        self.assertEqual(request["volume"], 1)
        self.assertFalse(output["submission_allowed"])
        self.assertEqual(output["orders_submitted"], 0)
        self.assertEqual(len(registry.intents), 1)
        self.assertEqual(registry.broker_orders, {})

    def test_replay_with_registry_is_idempotent(self) -> None:
        alpha = records()
        first_output, registry = build_alpha_order_dry_run(
            alpha, exchange_id="SZSE"
        )
        second_output, replayed = build_alpha_order_dry_run(
            alpha, exchange_id="SZSE", registry=registry
        )

        self.assertEqual(second_output, first_output)
        self.assertIs(replayed, registry)

    def test_rejects_non_short_alpha_leg(self) -> None:
        alpha = records(quantity=1)

        with self.assertRaisesRegex(ValueError, "must be negative"):
            build_alpha_order_dry_run(alpha, exchange_id="SZSE")


if __name__ == "__main__":
    unittest.main()
