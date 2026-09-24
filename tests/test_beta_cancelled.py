from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill, OrderIntent, apply_confirmed_fills, bind_broker_order,
    empty_ledger, empty_order_registry, register_order_intent,
)
from sim_hedge.adapters.sim_trading import (
    BrokerOrder, ConfirmedTradePage,
)
from sim_hedge.execution.alpha.reconcile import reconcile_alpha_state
from sim_hedge.execution.beta.cancelled import verify_and_retire_cancelled_beta
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.portfolio import AccountSnapshot, ActiveOrderSnapshot, PortfolioSnapshot, PositionSnapshot


NOW = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)


def case():
    registry = empty_order_registry("A1")
    for client, strategy, instrument, quantity, order_id in (
        ("alpha", "ALPHA", "A", -1, "OA"),
        ("beta-filled", "BETA", "B", 1, "OB"),
        ("beta-cancelled", "BETA", "C", 1, "OC"),
    ):
        registry = register_order_intent(registry, OrderIntent(
            client, "A1", strategy, "SZSE", instrument, quantity,
            "OPEN", "COUNTERPARTY", None, NOW,
            "P1" if strategy == "BETA" else None,
        ))
        registry = bind_broker_order(registry, client_order_id=client, order_id=order_id)
    fills = (
        ConfirmedFill("TA", "OA", "A1", "ALPHA", "A", -1, Decimal("0.1"), NOW),
        ConfirmedFill("TB", "OB", "A1", "BETA", "B", 1, Decimal("0.2"), NOW),
    )
    ledger = apply_confirmed_fills(empty_ledger("A1"), fills)
    portfolio = PortfolioSnapshot(
        AccountSnapshot("A1", "ETF_OPTION", "NORMAL", date(2026, 9, 22), "NORMAL"),
        (
            PositionSnapshot("PA", "A1", "A", "SHORT", Decimal(1), Decimal(1), Decimal(0), Decimal(0), Decimal(1)),
            PositionSnapshot("PB", "A1", "B", "LONG", Decimal(1), Decimal(1), Decimal(0), Decimal(0), Decimal(1)),
        ), (),
    )
    cancelled = BrokerOrder(
        "OC", "beta-cancelled", "A1", "SZSE", "C", "BUY", "OPEN",
        "COUNTERPARTY", Decimal("0.1"), 1, 0, 0, 1, "CANCELLED", NOW,
    )
    report = {
        "account_id": "A1", "registry_revision": registry.revision,
        "ledger_revision": ledger.revision, "position_match": True,
        "account_healthy": True, "active_order_ids": [],
        "unresolved_submissions": [], "unbound_order_intents": [],
        "managed_order_statuses": {"OA": "FILLED", "OB": "FILLED", "OC": "CANCELLED"},
    }
    return registry, ledger, portfolio, fills, cancelled, report


class Source:
    def __init__(self, portfolio, fills, order):
        self.portfolio = portfolio
        self.fills = fills
        self.order = order

    def load(self, account_id):
        return self.portfolio

    def load_order(self, order_id, account_id):
        return self.order

    def load_confirmed_trade_page(self, *args, **kwargs):
        return ConfirmedTradePage(self.fills, None, False)


class CancelledBetaTests(unittest.TestCase):
    def test_retirement_preserves_order_binding_and_restores_safe_reconciliation(self):
        registry, ledger, portfolio, fills, order, report = case()
        updated, audit = verify_and_retire_cancelled_beta(
            Source(portfolio, fills, order), registry, ledger, report, "OC"
        )
        self.assertEqual(updated.broker_orders["OC"], "beta-cancelled")
        self.assertEqual(updated.retired_cancelled_client_order_ids, ("beta-cancelled",))
        self.assertEqual(audit["orders_submitted"], 0)
        roundtrip = registry_from_payload(registry_to_payload(updated))
        self.assertEqual(roundtrip, updated)
        result = reconcile_alpha_state(
            registry=updated, ledger=ledger, fills=fills,
            orders=(order,), portfolio=portfolio, previous_registry=updated,
        )
        self.assertTrue(result.report["safe_for_hedging"])
        next_intent = OrderIntent(
            "beta-next", "A1", "BETA", "SZSE", "D", 1,
            "OPEN", "COUNTERPARTY", None, NOW, "P2",
        )
        self.assertEqual(
            register_order_intent(updated, next_intent).retired_cancelled_client_order_ids,
            ("beta-cancelled",),
        )

    def test_partial_fill_is_not_retired(self):
        registry, ledger, portfolio, fills, order, report = case()
        from dataclasses import replace
        partial = replace(order, status="PARTIALLY_CANCELLED", traded_volume=1, cancelled_volume=0)
        with self.assertRaisesRegex(ValueError, "not terminal CANCELLED with zero fills"):
            verify_and_retire_cancelled_beta(
                Source(portfolio, fills, partial), registry, ledger, report, "OC"
            )

    def test_broker_trade_for_cancelled_order_is_not_retired(self):
        registry, ledger, portfolio, fills, order, report = case()
        extra = ConfirmedFill("TC", "OC", "A1", "BETA", "C", 1, Decimal("0.1"), NOW)
        with self.assertRaisesRegex(ValueError, "broker reports a fill"):
            verify_and_retire_cancelled_beta(
                Source(portfolio, (*fills, extra), order), registry, ledger, report, "OC"
            )

    def test_stale_report_or_position_mismatch_blocks(self):
        registry, ledger, portfolio, fills, order, report = case()
        report["registry_revision"] -= 1
        with self.assertRaisesRegex(ValueError, "stale"):
            verify_and_retire_cancelled_beta(
                Source(portfolio, fills, order), registry, ledger, report, "OC"
            )
        report["registry_revision"] = registry.revision
        mismatched = PortfolioSnapshot(portfolio.account, portfolio.positions[:1], ())
        with self.assertRaisesRegex(ValueError, "broker positions differ"):
            verify_and_retire_cancelled_beta(
                Source(mismatched, fills, order), registry, ledger, report, "OC"
            )

    def test_active_broker_order_blocks(self):
        registry, ledger, portfolio, fills, order, report = case()
        active = ActiveOrderSnapshot(
            "OX", "A1", "D", "ACCEPTED", "BUY",
            Decimal(1), Decimal(0), Decimal(1),
        )
        source = Source(
            PortfolioSnapshot(portfolio.account, portfolio.positions, (active,)),
            fills, order,
        )
        with self.assertRaisesRegex(ValueError, "active orders"):
            verify_and_retire_cancelled_beta(source, registry, ledger, report, "OC")


if __name__ == "__main__":
    unittest.main()
