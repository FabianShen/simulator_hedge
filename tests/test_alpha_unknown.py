from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill, OrderIntent, apply_confirmed_fills, bind_broker_order,
    empty_ledger, empty_order_registry, mark_submission_unknown,
    register_order_intent,
)
from sim_hedge.adapters.sim_trading import (
    BrokerOrder, BrokerOrderPage, BrokerTradeOrderPage,
)
from sim_hedge.execution.alpha.unknown import verify_and_retire
from sim_hedge.domain.portfolio import AccountSnapshot, PortfolioSnapshot, PositionSnapshot


def case():
    now = datetime(2026, 9, 22, 1, 54, tzinfo=timezone.utc)
    registry = empty_order_registry("A1")
    for client, instrument in (("filled", "C1"), ("unknown", "P1")):
        registry = register_order_intent(registry, OrderIntent(
            client, "A1", "ALPHA", "SZSE", instrument, -2,
            "OPEN", "COUNTERPARTY", None, now,
        ))
    registry = bind_broker_order(registry, client_order_id="filled", order_id="O1")
    registry = mark_submission_unknown(registry, "unknown")
    ledger = apply_confirmed_fills(empty_ledger("A1"), (
        ConfirmedFill("T1", "O1", "A1", "ALPHA", "C1", -2, Decimal("0.1"), now),
    ))
    return registry, ledger, now


def preparing_report():
    return {
        "account_id": "A1",
        "unknown": [{
            "client_order_id": "unknown",
            "error": 'simulator HTTP 503; submission outcome may be unknown: '
                     '{"success":false,"error_code":"ORDER_MARKET_DATA_PREPARING"}',
        }],
    }


class Source:
    def __init__(self, now):
        self.now = now
        self.order_client = "filled"
        self.trade_ids = ("O1",)
        self.position_volume = Decimal(2)
        self.active_orders = ()

    def load(self, account_id):
        return PortfolioSnapshot(
            AccountSnapshot(
                "A1", "ETF_OPTION", "NORMAL", date(2026, 9, 22),
                "NORMAL", Decimal("1000000"),
            ),
            (PositionSnapshot(
                "P1", "A1", "C1", "SHORT", self.position_volume,
                self.position_volume, Decimal(0), Decimal(0), Decimal(1),
            ),), self.active_orders,
        )

    def load_order_page(self, *args, **kwargs):
        return BrokerOrderPage((BrokerOrder(
            "O1", self.order_client, "A1", "SZSE", "C1", "SELL", "OPEN",
            "COUNTERPARTY", Decimal("0.1"), 2, 2, 0, 0, "FILLED", self.now,
        ),), None, False)

    def load_trade_order_ids_page(self, *args, **kwargs):
        return BrokerTradeOrderPage(self.trade_ids, None, False)


class UnknownAlphaTests(unittest.TestCase):
    def test_absent_order_can_be_retired_without_touching_positions(self):
        registry, ledger, now = case()
        updated, audit = verify_and_retire(Source(now), registry, ledger, "unknown", preparing_report())
        self.assertEqual(updated.unknown_client_order_ids, ())
        self.assertIn("unknown", updated.abandoned_client_order_ids)
        self.assertEqual(ledger.alpha_positions, {"C1": -2})
        self.assertEqual(audit["orders_submitted"], 0)

    def test_matching_broker_order_blocks_retirement(self):
        registry, ledger, now = case()
        source = Source(now)
        source.order_client = "unknown"
        with self.assertRaisesRegex(ValueError, "broker order exists"):
            verify_and_retire(source, registry, ledger, "unknown", preparing_report())
        self.assertEqual(registry.unknown_client_order_ids, ("unknown",))

    def test_unowned_trade_blocks_retirement(self):
        registry, ledger, now = case()
        source = Source(now)
        source.trade_ids = ("O1", "O2")
        with self.assertRaisesRegex(ValueError, "trades for orders outside"):
            verify_and_retire(source, registry, ledger, "unknown", preparing_report())

    def test_position_mismatch_blocks_retirement(self):
        registry, ledger, now = case()
        source = Source(now)
        source.position_volume = Decimal(3)
        with self.assertRaisesRegex(ValueError, "broker positions differ"):
            verify_and_retire(source, registry, ledger, "unknown", preparing_report())

    def test_generic_timeout_cannot_be_retired_by_absence_check(self):
        registry, ledger, now = case()
        report = preparing_report()
        report["unknown"][0]["error"] = "simulator connection failed; outcome may be unknown"
        with self.assertRaisesRegex(ValueError, "not a documented"):
            verify_and_retire(Source(now), registry, ledger, "unknown", report)


if __name__ == "__main__":
    unittest.main()
