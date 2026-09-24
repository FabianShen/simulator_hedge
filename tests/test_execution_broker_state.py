import unittest
from datetime import datetime, timezone
from decimal import Decimal

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
from sim_hedge.state.execution_state import assess_broker_execution
from sim_hedge.domain.portfolio import (
    AccountSnapshot,
    ActiveOrderSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)


NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)


def proposal(*, trade=3):
    return build_hedge_proposal(
        pricing_request_id="pricing-1",
        account_id="A1",
        base_ledger_revision=0,
        created_at=NOW,
        engine_name="test",
        engine_version="1",
        confirmed_beta_positions={},
        incremental_trades={"CALL": trade},
    )


def beta_intent(proposal_id: str, quantity: int = 3):
    return OrderIntent(
        client_order_id=f"beta-{proposal_id}",
        account_id="A1",
        strategy="BETA",
        exchange_id="SZSE",
        instrument="CALL",
        quantity=quantity,
        offset="OPEN",
        order_type="LIMIT",
        limit_price=Decimal("0.1000"),
        created_at=NOW,
        proposal_id=proposal_id,
    )


def active_order(order_id: str, remaining: int = 2):
    return ActiveOrderSnapshot(
        order_id=order_id,
        account_id="A1",
        instrument="CALL",
        status="ACTIVE",
        direction="BUY",
        total_volume=Decimal(3),
        traded_volume=Decimal(3 - remaining),
        remaining_volume=Decimal(remaining),
        limit_price=Decimal("0.1000"),
    )


def portfolio(*orders, position_quantity=0):
    positions = ()
    if position_quantity:
        positions = (
            PositionSnapshot(
                position_id="P1",
                account_id="A1",
                instrument="CALL",
                direction="LONG" if position_quantity > 0 else "SHORT",
                volume=Decimal(abs(position_quantity)),
                today_volume=Decimal(abs(position_quantity)),
                yesterday_volume=Decimal(0),
                frozen_volume=Decimal(0),
                available_volume=Decimal(abs(position_quantity)),
            ),
        )
    return PortfolioSnapshot(
        account=AccountSnapshot("A1", "ETF_OPTION", "NORMAL"),
        positions=positions,
        active_orders=tuple(orders),
    )


class BrokerExecutionStateTests(unittest.TestCase):
    def test_maps_confirmed_fill_and_working_remainder_by_proposal(self) -> None:
        value = proposal()
        intent = beta_intent(value["proposal_id"])
        registry = register_order_intent(empty_order_registry("A1"), intent)
        registry = bind_broker_order(
            registry, client_order_id=intent.client_order_id, order_id="O1"
        )
        ledger = apply_confirmed_fills(
            empty_ledger("A1"),
            (
                ConfirmedFill(
                    trade_id="T1",
                    order_id="O1",
                    account_id="A1",
                    strategy="BETA",
                    instrument="CALL",
                    quantity=1,
                    price=Decimal("0.1000"),
                    executed_at=NOW,
                ),
            ),
        )

        result = assess_broker_execution(
            value,
            ledger=ledger,
            registry=registry,
            portfolio=portfolio(active_order("O1"), position_quantity=1),
        )

        self.assertEqual(result.status, "WORKING")
        self.assertEqual(result.confirmed_fills, {"CALL": 1})
        self.assertEqual(result.working_trades, {"CALL": 2})
        self.assertEqual(result.uncovered_trades, {})

    def test_old_proposal_working_order_blocks_new_proposal(self) -> None:
        current = proposal()
        intent = beta_intent("hedge-older")
        registry = register_order_intent(empty_order_registry("A1"), intent)
        registry = bind_broker_order(
            registry, client_order_id=intent.client_order_id, order_id="O1"
        )

        result = assess_broker_execution(
            current,
            ledger=empty_ledger("A1"),
            registry=registry,
            portfolio=portfolio(active_order("O1", remaining=3)),
        )

        self.assertEqual(result.status, "BLOCKED_BY_PRIOR_PROPOSAL")
        self.assertEqual(
            result.cancellation_candidate_ids, (intent.client_order_id,)
        )

    def test_rejects_unregistered_active_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "not registered"):
            assess_broker_execution(
                proposal(),
                ledger=empty_ledger("A1"),
                registry=empty_order_registry("A1"),
                portfolio=portfolio(active_order("UNKNOWN", remaining=3)),
            )

    def test_rejects_beta_change_from_another_proposal(self) -> None:
        value = proposal()
        old = beta_intent("hedge-older", quantity=1)
        registry = register_order_intent(empty_order_registry("A1"), old)
        registry = bind_broker_order(
            registry, client_order_id=old.client_order_id, order_id="OLD"
        )
        ledger = apply_confirmed_fills(
            empty_ledger("A1"),
            (
                ConfirmedFill(
                    "T-OLD",
                    "OLD",
                    "A1",
                    "BETA",
                    "CALL",
                    1,
                    Decimal("0.1"),
                    NOW,
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "outside this proposal"):
            assess_broker_execution(
                value,
                ledger=ledger,
                registry=registry,
                portfolio=portfolio(position_quantity=1),
            )

    def test_rejects_partial_trade_missing_from_ledger(self) -> None:
        value = proposal()
        intent = beta_intent(value["proposal_id"])
        registry = register_order_intent(empty_order_registry("A1"), intent)
        registry = bind_broker_order(
            registry, client_order_id=intent.client_order_id, order_id="O1"
        )

        with self.assertRaisesRegex(ValueError, "unreconciled fills"):
            assess_broker_execution(
                value,
                ledger=empty_ledger("A1"),
                registry=registry,
                portfolio=portfolio(active_order("O1")),
            )


if __name__ == "__main__":
    unittest.main()
