from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    bind_broker_order,
    empty_ledger,
    empty_order_registry,
    mark_submission_unknown,
    register_order_intent,
)
from sim_hedge.adapters.sim_trading import BrokerOrder
from sim_hedge.alpha_reconcile import recover_order_bindings, reconcile_alpha_state
from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot, PositionSnapshot


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


def intent() -> OrderIntent:
    return OrderIntent(
        client_order_id="alpha-1",
        account_id="A1",
        strategy="ALPHA",
        exchange_id="SZSE",
        instrument="9001",
        quantity=-2,
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("0.1234"),
        created_at=NOW,
    )


def broker_order() -> BrokerOrder:
    return BrokerOrder(
        order_id="O1",
        client_order_id="alpha-1",
        account_id="A1",
        exchange_id="SZSE",
        instrument="9001",
        direction="SELL",
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("0.1234"),
        total_volume=2,
        traded_volume=2,
        remaining_volume=0,
        cancelled_volume=0,
        status="FILLED",
        created_at=NOW,
    )


def portfolio(quantity: int = -2) -> PortfolioSnapshot:
    positions = ()
    if quantity:
        volume = Decimal(abs(quantity))
        positions = (
            PositionSnapshot(
                position_id="P1",
                account_id="A1",
                instrument="9001",
                direction="SHORT" if quantity < 0 else "LONG",
                volume=volume,
                today_volume=volume,
                yesterday_volume=Decimal(0),
                frozen_volume=Decimal(0),
                available_volume=volume,
            ),
        )
    return PortfolioSnapshot(
        account=AccountSnapshot(
            account_id="A1",
            account_type="ETF_OPTION",
            status="NORMAL",
            trading_day=date(2026, 9, 18),
            risk_state="NORMAL",
        ),
        positions=positions,
        active_orders=(),
    )


class AlphaReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registered = register_order_intent(empty_order_registry("A1"), intent())

    def test_recovers_unknown_submission_from_exact_broker_order(self) -> None:
        unknown = mark_submission_unknown(self.registered, "alpha-1")

        recovered = recover_order_bindings(unknown, (broker_order(),))

        self.assertEqual(recovered.broker_orders, {"O1": "alpha-1"})
        self.assertEqual(recovered.unknown_client_order_ids, ())

    def test_rejects_broker_order_that_conflicts_with_saved_intent(self) -> None:
        conflict = BrokerOrder(
            **{**broker_order().__dict__, "total_volume": 3}
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            recover_order_bindings(self.registered, (conflict,))

    def test_applies_fill_once_and_verifies_broker_position(self) -> None:
        bound = bind_broker_order(
            self.registered, client_order_id="alpha-1", order_id="O1"
        )
        fill = ConfirmedFill(
            trade_id="T1",
            order_id="O1",
            account_id="A1",
            strategy="ALPHA",
            instrument="9001",
            quantity=-2,
            price=Decimal("0.1234"),
            executed_at=NOW,
        )

        result = reconcile_alpha_state(
            registry=bound,
            ledger=empty_ledger("A1"),
            fills=(fill,),
            orders=(broker_order(),),
            portfolio=portfolio(),
            previous_registry=bound,
        )

        self.assertTrue(result.report["safe_for_hedging"])
        self.assertTrue(result.report["position_match"])
        self.assertEqual(result.ledger.alpha_positions, {"9001": -2})

    def test_unsubmitted_alpha_intent_is_not_ready(self) -> None:
        result = reconcile_alpha_state(
            registry=self.registered,
            ledger=empty_ledger("A1"),
            fills=(),
            orders=(),
            portfolio=portfolio(0),
            previous_registry=self.registered,
        )

        self.assertFalse(result.report["safe_for_hedging"])
        self.assertEqual(result.report["unbound_alpha_intents"], ["alpha-1"])

    def test_bound_but_unfilled_alpha_order_is_not_ready(self) -> None:
        bound = bind_broker_order(
            self.registered, client_order_id="alpha-1", order_id="O1"
        )

        result = reconcile_alpha_state(
            registry=bound,
            ledger=empty_ledger("A1"),
            fills=(),
            orders=(broker_order(),),
            portfolio=portfolio(0),
            previous_registry=bound,
        )

        self.assertFalse(result.report["safe_for_hedging"])
        self.assertFalse(result.report["alpha_fill_complete"])


if __name__ == "__main__":
    unittest.main()
