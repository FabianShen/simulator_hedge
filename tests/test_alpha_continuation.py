from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    abandon_unsubmitted_intents,
    apply_confirmed_fills,
    bind_broker_order,
    empty_ledger,
    empty_order_registry,
    register_order_intent,
)
from sim_hedge.execution.alpha.continuation import (
    assess_alpha_continuation,
    select_alpha_continuation_request,
)


def inputs(status: str = "PARTIALLY_FILLED"):
    account_id = "A1"
    instrument = "C34"
    now = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
    registry = register_order_intent(
        empty_order_registry(account_id),
        OrderIntent(
            "alpha-1", account_id, "ALPHA", "SZSE", instrument, -752,
            "OPEN", "COUNTERPARTY", None, now,
        ),
    )
    registry = bind_broker_order(registry, client_order_id="alpha-1", order_id="O1")
    ledger = apply_confirmed_fills(
        empty_ledger(account_id),
        (ConfirmedFill("T1", "O1", account_id, "ALPHA", instrument, -702, Decimal("0.1"), now),),
    )
    plan = {
        "plan_id": "P1", "account_id": account_id,
        "contracts_per_option": 752,
        "legs": [{"instrument": instrument, "quantity": -752}],
    }
    reconciliation = {
        "account_id": account_id,
        "registry_revision": registry.revision,
        "ledger_revision": ledger.revision,
        "position_match": True,
        "account_healthy": True,
        "unresolved_submissions": [],
        "unbound_order_intents": [],
        "broker_positions": {instrument: -702},
        "managed_order_statuses": {"O1": status},
    }
    return plan, registry, ledger, reconciliation


class AlphaContinuationTests(unittest.TestCase):
    def test_partial_order_blocks_top_up_even_if_snapshot_has_no_active_orders(self):
        report = assess_alpha_continuation(*inputs())

        self.assertEqual(report["status"], "WAITING_FOR_BROKER")
        self.assertEqual(report["remaining_sell_contracts"], {"C34": 50})
        self.assertEqual(report["working_order_ids"], ["O1"])
        self.assertEqual(report["orders_submitted"], 0)

    def test_terminal_partial_order_reports_exact_shortfall(self):
        report = assess_alpha_continuation(*inputs("PARTIALLY_CANCELLED"))

        self.assertEqual(report["status"], "NEEDS_TOP_UP")
        self.assertEqual(report["total_remaining_contracts"], 50)

    def test_stale_reconciliation_is_rejected(self):
        plan, registry, ledger, reconciliation = inputs()
        reconciliation["ledger_revision"] -= 1

        with self.assertRaisesRegex(ValueError, "stale"):
            assess_alpha_continuation(plan, registry, ledger, reconciliation)

    def test_abandoned_intent_from_older_plan_does_not_change_target(self):
        plan, registry, ledger, reconciliation = inputs("PARTIALLY_CANCELLED")
        registry = register_order_intent(
            registry,
            OrderIntent(
                "alpha-abandoned", "A1", "ALPHA", "SZSE", "C35", -752,
                "OPEN", "COUNTERPARTY", None,
                datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc),
            ),
        )
        registry = abandon_unsubmitted_intents(registry, ("alpha-abandoned",))
        reconciliation["registry_revision"] = registry.revision

        report = assess_alpha_continuation(plan, registry, ledger, reconciliation)

        self.assertEqual(report["remaining_sell_contracts"], {"C34": 50})

    def test_selects_unbound_intent_without_inspecting_bid_or_ask(self):
        plan, registry, ledger, reconciliation = inputs("FILLED")
        now = datetime.now(timezone.utc)
        registry = register_order_intent(
            registry,
            OrderIntent(
                "alpha-2", "A1", "ALPHA", "SZSE", "P32", -752,
                "OPEN", "COUNTERPARTY", None, now,
            ),
        )
        plan["legs"].append({"instrument": "P32", "quantity": -752})
        reconciliation["registry_revision"] = registry.revision
        reconciliation["unbound_order_intents"] = ["alpha-2"]
        assessment = assess_alpha_continuation(plan, registry, ledger, reconciliation)
        market = {
            "requestId": "M2",
            "asOf": now.isoformat(),
            "options": [{"instrument": "P32", "bid": None, "ask": None}],
        }

        request, status = select_alpha_continuation_request(
            assessment, market, registry, now=now, max_age=10,
        )

        self.assertEqual(status, "READY")
        self.assertEqual(request["client_order_id"], "alpha-2")
        self.assertEqual(request["volume"], 752)

        request, status = select_alpha_continuation_request(
            assessment, market, registry, now=now, max_age=10,
        )
        self.assertEqual(request["client_order_id"], "alpha-2")
        self.assertEqual(status, "READY")

        request, status = select_alpha_continuation_request(
            assessment, market, registry, now=now, max_age=10,
            retry_after={"alpha-2": now + timedelta(seconds=30)},
        )
        self.assertIsNone(request)
        self.assertEqual(status, "WAITING_FOR_RETRY")

        with self.assertRaisesRegex(ValueError, "stale"):
            select_alpha_continuation_request(
                assessment, market, registry,
                now=now + timedelta(seconds=11), max_age=10,
            )


if __name__ == "__main__":
    unittest.main()
