from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from hedge_engine import (
    ConfirmedFill,
    OrderIntent,
    bind_broker_order,
    apply_confirmed_fills,
    empty_ledger,
    empty_order_registry,
    mark_submission_unknown,
    register_order_intent,
)
from sim_hedge.adapters.sim_trading import BrokerOrder
from sim_hedge.execution.alpha.reconcile import recover_order_bindings, reconcile_alpha_state
from sim_hedge.domain.portfolio import AccountSnapshot, PortfolioSnapshot, PositionSnapshot


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

    def test_counterparty_order_may_report_resolved_execution_price(self) -> None:
        counterparty_intent = replace(intent(), order_type="COUNTERPARTY", limit_price=None)
        registered = register_order_intent(empty_order_registry("A1"), counterparty_intent)
        resolved = replace(
            broker_order(), order_type="COUNTERPARTY", limit_price=Decimal("0.0002")
        )

        recovered = recover_order_bindings(registered, (resolved,))

        self.assertEqual(recovered.broker_orders, {"O1": "alpha-1"})

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
        self.assertEqual(result.report["unbound_order_intents"], ["alpha-1"])

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

    def test_beta_fill_must_reconcile_before_next_hedge(self) -> None:
        registry = bind_broker_order(
            self.registered, client_order_id="alpha-1", order_id="O1"
        )
        beta_intent = replace(
            intent(),
            client_order_id="beta-1",
            strategy="BETA",
            instrument="9002",
            quantity=1,
        )
        registry = register_order_intent(registry, beta_intent)
        registry = bind_broker_order(
            registry, client_order_id="beta-1", order_id="O2"
        )
        alpha_fill = ConfirmedFill(
            trade_id="T1",
            order_id="O1",
            account_id="A1",
            strategy="ALPHA",
            instrument="9001",
            quantity=-2,
            price=Decimal("0.1234"),
            executed_at=NOW,
        )
        beta_fill = ConfirmedFill(
            trade_id="T2",
            order_id="O2",
            account_id="A1",
            strategy="BETA",
            instrument="9002",
            quantity=1,
            price=Decimal("0.2000"),
            executed_at=NOW,
        )
        ledger = apply_confirmed_fills(empty_ledger("A1"), (alpha_fill,))
        alpha_position = portfolio().positions[0]
        beta_position = replace(
            alpha_position,
            position_id="P2",
            instrument="9002",
            direction="LONG",
            volume=Decimal(1),
            today_volume=Decimal(1),
            available_volume=Decimal(1),
        )
        broker = PortfolioSnapshot(
            account=portfolio().account,
            positions=(alpha_position, beta_position),
            active_orders=(),
        )
        beta_order = replace(
            broker_order(),
            order_id="O2",
            client_order_id="beta-1",
            instrument="9002",
            direction="BUY",
            total_volume=1,
            traded_volume=1,
            limit_price=Decimal("0.2000"),
        )

        result = reconcile_alpha_state(
            registry=registry,
            ledger=ledger,
            fills=(beta_fill,),
            orders=(broker_order(), beta_order),
            portfolio=broker,
            previous_registry=registry,
        )

        self.assertTrue(result.report["safe_for_hedging"])
        self.assertTrue(result.report["beta_fill_complete"])
        self.assertTrue(result.report["all_fills_complete"])


if __name__ == "__main__":
    unittest.main()
