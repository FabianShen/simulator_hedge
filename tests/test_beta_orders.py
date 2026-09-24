from datetime import datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    apply_confirmed_fills,
    bind_broker_order,
    build_hedge_proposal,
    empty_ledger,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.execution.beta.orders import build_beta_order_dry_run


NOW = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)


def state(beta_quantity: int = 0, alpha_quantity: int = -1):
    alpha_intent = OrderIntent(
        client_order_id="alpha-1",
        account_id="A1",
        strategy="ALPHA",
        exchange_id="SZSE",
        instrument="ALPHA",
        quantity=alpha_quantity,
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
            quantity=alpha_quantity,
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
            order_type="COUNTERPARTY",
            limit_price=None,
            created_at=NOW,
            proposal_id="hedge-old",
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
    return apply_confirmed_fills(empty_ledger("A1"), fills), registry


def accepted_proposal(trades=None, revision=1, beta_positions=None, request_id="R1"):
    return {
        **build_hedge_proposal(
            pricing_request_id=request_id,
            account_id="A1",
            base_ledger_revision=revision,
            created_at=NOW,
            engine_name="test",
            engine_version="1",
            confirmed_beta_positions=beta_positions or {},
            incremental_trades=(
                {"CALL": 2, "PUT": -3} if trades is None else trades
            ),
        ),
        "source_market_as_of": NOW.isoformat(),
        "execution_batch_version": "sim-hedge/execution-batch/v1",
        "submission_allowed": False,
    }


class BetaOrderDryRunTests(unittest.TestCase):
    def test_registers_counterparty_intents_without_price_or_submission(self) -> None:
        ledger, registry = state()
        hedge = accepted_proposal(revision=ledger.revision)

        output, updated = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=5
        )

        self.assertEqual(
            [(item["direction"], item["offset_flag"], item["volume"]) for item in output["requests"]],
            [("BUY", "OPEN", 2), ("SELL", "OPEN", 3)],
        )
        self.assertEqual(output["order_type"], "COUNTERPARTY")
        self.assertFalse(output["submission_allowed"])
        self.assertTrue(all("limit_price" not in item for item in output["requests"]))
        beta = [value for value in updated.intents.values() if value.strategy == "BETA"]
        self.assertTrue(all(value.proposal_id == hedge["proposal_id"] for value in beta))

    def test_splits_trade_that_crosses_through_zero(self) -> None:
        ledger, registry = state(beta_quantity=2)
        hedge = accepted_proposal(
            {"CALL": -5}, revision=ledger.revision, beta_positions={"CALL": 2}
        )

        output, _ = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=5
        )

        self.assertEqual(
            [(item["offset_flag"], item["volume"]) for item in output["requests"]],
            [("CLOSE", 2), ("OPEN", 3)],
        )
        self.assertEqual(output["projected_beta_positions"], {"CALL": -3})

    def test_empty_increment_is_no_action(self) -> None:
        ledger, registry = state()
        hedge = accepted_proposal({}, revision=ledger.revision)

        output, updated = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=1
        )

        self.assertEqual(output["requests"], [])
        self.assertEqual(output["total_contracts"], 0)
        self.assertEqual(updated, registry)

    def test_replay_is_idempotent(self) -> None:
        ledger, registry = state()
        hedge = accepted_proposal(revision=ledger.revision)
        first, registered = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=5
        )

        second, replayed = build_beta_order_dry_run(
            hedge, ledger, registered, exchange_id="SZSE", max_total_contracts=5
        )

        self.assertEqual(second, first)
        self.assertEqual(replayed, registered)

    def test_explicitly_abandons_stale_unsubmitted_proposal(self) -> None:
        ledger, registry = state()
        first_hedge = accepted_proposal(revision=ledger.revision)
        first, registered = build_beta_order_dry_run(
            first_hedge,
            ledger,
            registry,
            exchange_id="SZSE",
            max_total_contracts=5,
        )
        second_hedge = accepted_proposal(revision=ledger.revision, request_id="R2")

        second, refreshed = build_beta_order_dry_run(
            second_hedge,
            ledger,
            registered,
            exchange_id="SZSE",
            max_total_contracts=5,
            replace_unsubmitted=True,
        )

        old_ids = {item["client_order_id"] for item in first["requests"]}
        new_ids = {item["client_order_id"] for item in second["requests"]}
        self.assertTrue(old_ids.isdisjoint(new_ids))
        self.assertEqual(set(refreshed.abandoned_client_order_ids), old_ids)

    def test_beta_trade_on_alpha_instrument_uses_broker_net_for_close(self) -> None:
        ledger, registry = state(alpha_quantity=-10)
        hedge = accepted_proposal({"ALPHA": 3}, revision=ledger.revision)

        output, _ = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=3
        )

        self.assertEqual(output["projected_beta_positions"], {"ALPHA": 3})
        self.assertEqual(output["requests"][0]["direction"], "BUY")
        self.assertEqual(output["requests"][0]["offset_flag"], "CLOSE")

    def test_same_direction_beta_trade_on_alpha_instrument_is_open(self) -> None:
        ledger, registry = state(alpha_quantity=-10)
        hedge = accepted_proposal({"ALPHA": -3}, revision=ledger.revision)

        output, _ = build_beta_order_dry_run(
            hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=3
        )

        self.assertEqual(output["requests"][0]["direction"], "SELL")
        self.assertEqual(output["requests"][0]["offset_flag"], "OPEN")

    def test_rejects_beta_target_above_alpha_thirty_percent(self) -> None:
        ledger, registry = state(alpha_quantity=-10)
        hedge = accepted_proposal({"ALPHA": 4}, revision=ledger.revision)

        with self.assertRaisesRegex(ValueError, "exceeds 30%"):
            build_beta_order_dry_run(
                hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=4
            )

    def test_requires_explicit_volume_limit(self) -> None:
        ledger, registry = state()
        hedge = accepted_proposal(revision=ledger.revision)

        with self.assertRaisesRegex(ValueError, "exceeds explicit limit"):
            build_beta_order_dry_run(
                hedge, ledger, registry, exchange_id="SZSE", max_total_contracts=4
            )


if __name__ == "__main__":
    unittest.main()
