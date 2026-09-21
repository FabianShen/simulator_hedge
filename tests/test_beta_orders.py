from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    apply_confirmed_fills,
    bind_broker_order,
    empty_ledger,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.beta_orders import build_beta_order_dry_run
from hedge_engine import build_hedge_proposal


NOW = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)


def state(beta_quantity: int = 0):
    alpha_intent = OrderIntent(
        client_order_id="alpha-1",
        account_id="A1",
        strategy="ALPHA",
        exchange_id="SZSE",
        instrument="ALPHA",
        quantity=-1,
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("0.1000"),
        created_at=NOW,
    )
    registry = register_order_intent(empty_order_registry("A1"), alpha_intent)
    registry = bind_broker_order(
        registry, client_order_id="alpha-1", order_id="OA"
    )
    fills = [
        ConfirmedFill(
            trade_id="TA",
            order_id="OA",
            account_id="A1",
            strategy="ALPHA",
            instrument="ALPHA",
            quantity=-1,
            price=Decimal("0.1000"),
            executed_at=NOW,
        )
    ]
    if beta_quantity:
        beta_intent = OrderIntent(
            client_order_id="beta-old",
            account_id="A1",
            strategy="BETA",
            exchange_id="SZSE",
            instrument="CALL",
            quantity=beta_quantity,
            offset="OPEN",
            order_type="LIMIT",
            limit_price=Decimal("0.2000"),
            created_at=NOW,
        )
        registry = register_order_intent(registry, beta_intent)
        registry = bind_broker_order(
            registry, client_order_id="beta-old", order_id="OB"
        )
        fills.append(
            ConfirmedFill(
                trade_id="TB",
                order_id="OB",
                account_id="A1",
                strategy="BETA",
                instrument="CALL",
                quantity=beta_quantity,
                price=Decimal("0.2000"),
                executed_at=NOW,
            )
        )
    ledger = apply_confirmed_fills(empty_ledger("A1"), fills)
    return ledger, registry


def inputs(trades=None, revision=1, beta_positions=None, request_id="R1"):
    pricing = {
        "requestId": request_id,
        "asOf": "2026-09-21T02:00:00Z",
        "options": [
            {"instrument": "CALL", "marketPrice": "0.20125", "priceTick": "0.0001"},
            {"instrument": "PUT", "marketPrice": "0.15125", "priceTick": "0.0001"},
            {"instrument": "ALPHA", "marketPrice": "0.1000", "priceTick": "0.0001"},
        ],
    }
    hedge = {
        **build_hedge_proposal(
            pricing_request_id=request_id,
            account_id="A1",
            base_ledger_revision=revision,
            created_at=NOW,
            engine_name="test",
            engine_version="1",
            confirmed_beta_positions=beta_positions or {},
            incremental_trades=trades or {"CALL": 2, "PUT": -3},
        ),
        "hedge_universe": ["CALL", "PUT"],
    }
    return pricing, hedge


class BetaOrderDryRunTests(unittest.TestCase):
    def test_registers_open_beta_intents_without_submission(self) -> None:
        ledger, registry = state()
        pricing, hedge = inputs(revision=ledger.revision)

        output, updated = build_beta_order_dry_run(
            pricing,
            hedge,
            ledger,
            registry,
            exchange_id="SZSE",
            max_total_contracts=5,
        )

        self.assertEqual(
            [(item["direction"], item["offset_flag"], item["volume"]) for item in output["requests"]],
            [("BUY", "OPEN", 2), ("SELL", "OPEN", 3)],
        )
        self.assertFalse(output["submission_allowed"])
        self.assertEqual(output["orders_submitted"], 0)
        self.assertTrue(all(intent.strategy == "BETA" for key, intent in updated.intents.items() if key.startswith("beta-")))
        self.assertTrue(
            all(
                intent.proposal_id == hedge["proposal_id"]
                for key, intent in updated.intents.items()
                if key.startswith("beta-")
            )
        )

    def test_splits_a_trade_that_crosses_through_zero(self) -> None:
        ledger, registry = state(beta_quantity=2)
        pricing, hedge = inputs(
            {"CALL": -5}, revision=ledger.revision, beta_positions={"CALL": 2}
        )

        output, _ = build_beta_order_dry_run(
            pricing,
            hedge,
            ledger,
            registry,
            exchange_id="SZSE",
            max_total_contracts=5,
        )

        self.assertEqual(
            [(item["offset_flag"], item["volume"]) for item in output["requests"]],
            [("CLOSE", 2), ("OPEN", 3)],
        )
        self.assertEqual(output["projected_beta_positions"], {"CALL": -3})

    def test_replay_is_idempotent(self) -> None:
        ledger, registry = state()
        pricing, hedge = inputs(revision=ledger.revision)
        first, registered = build_beta_order_dry_run(
            pricing, hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=5
        )

        second, replayed = build_beta_order_dry_run(
            pricing, hedge, ledger, registered, exchange_id="SZSE", max_total_contracts=5
        )

        self.assertEqual(second, first)
        self.assertEqual(replayed, registered)

    def test_explicitly_abandons_stale_unsubmitted_beta_proposal(self) -> None:
        ledger, registry = state()
        pricing, hedge = inputs(revision=ledger.revision)
        first, registered = build_beta_order_dry_run(
            pricing, hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=5
        )
        refreshed_pricing, refreshed_hedge = inputs(
            revision=ledger.revision, request_id="R2"
        )

        second, refreshed = build_beta_order_dry_run(
            refreshed_pricing,
            refreshed_hedge,
            ledger,
            registered,
            exchange_id="SZSE",
            max_total_contracts=5,
            replace_unsubmitted=True,
        )

        old_ids = {request["client_order_id"] for request in first["requests"]}
        new_ids = {request["client_order_id"] for request in second["requests"]}
        self.assertTrue(old_ids.isdisjoint(new_ids))
        self.assertEqual(set(refreshed.abandoned_client_order_ids), old_ids)
        self.assertEqual(set(second["abandoned_previous_intents"]), old_ids)

    def test_rejects_alpha_instrument_as_beta_trade(self) -> None:
        ledger, registry = state()
        pricing, hedge = inputs({"ALPHA": 1}, revision=ledger.revision)

        with self.assertRaisesRegex(ValueError, "outside the hedge universe"):
            build_beta_order_dry_run(
                pricing, hedge, ledger, registry,
                exchange_id="SZSE", max_total_contracts=1,
            )

    def test_requires_explicit_volume_limit(self) -> None:
        ledger, registry = state()
        pricing, hedge = inputs(revision=ledger.revision)

        with self.assertRaisesRegex(ValueError, "exceeds explicit limit"):
            build_beta_order_dry_run(
                pricing, hedge, ledger, registry,
                exchange_id="SZSE", max_total_contracts=4,
            )


if __name__ == "__main__":
    unittest.main()
