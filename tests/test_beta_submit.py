from datetime import date, datetime, timedelta, timezone
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
from sim_hedge.adapters.sim_trading import SimTradingUnknownOutcomeError
from sim_hedge.beta_submit import submit_beta_orders
from sim_hedge.order_submission import request_for_intent
from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot, PositionSnapshot


NOW = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)


def setup():
    alpha = OrderIntent(
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
    beta = OrderIntent(
        client_order_id="beta-1",
        account_id="A1",
        strategy="BETA",
        exchange_id="SZSE",
        instrument="BETA",
        quantity=1,
        offset="OPEN",
        order_type="COUNTERPARTY",
        limit_price=None,
        created_at=NOW,
        proposal_id="hedge-example",
    )
    registry = register_order_intent(empty_order_registry("A1"), alpha)
    registry = bind_broker_order(
        registry, client_order_id="alpha-1", order_id="OA"
    )
    registry = register_order_intent(registry, beta)
    ledger = apply_confirmed_fills(
        empty_ledger("A1"),
        (
            ConfirmedFill(
                trade_id="TA",
                order_id="OA",
                account_id="A1",
                strategy="ALPHA",
                instrument="ALPHA",
                quantity=-1,
                price=Decimal("0.1000"),
                executed_at=NOW,
            ),
        ),
    )
    proposal = {
        "source_hedge_proposal_id": "hedge-example",
        "source_pricing_request_id": "R1",
        "source_market_as_of": NOW.isoformat(),
        "base_strategy_ledger_revision": ledger.revision,
        "strategy": "BETA",
        "order_type": "COUNTERPARTY",
        "submission_allowed": False,
        "orders_submitted": 0,
        "requests": [request_for_intent(beta)],
    }
    portfolio = PortfolioSnapshot(
        account=AccountSnapshot(
            account_id="A1",
            account_type="ETF_OPTION",
            status="NORMAL",
            trading_day=date(2026, 9, 21),
            risk_state="NORMAL",
        ),
        positions=(
            PositionSnapshot(
                position_id="PA",
                account_id="A1",
                instrument="ALPHA",
                direction="SHORT",
                volume=Decimal(1),
                today_volume=Decimal(1),
                yesterday_volume=Decimal(0),
                frozen_volume=Decimal(0),
                available_volume=Decimal(1),
            ),
        ),
        active_orders=(),
    )
    return proposal, registry, ledger, portfolio


class BetaSubmissionTests(unittest.TestCase):
    def test_submits_registered_beta_without_requiring_empty_portfolio(self) -> None:
        proposal, registry, ledger, portfolio = setup()
        persisted = []

        updated, report = submit_beta_orders(
            proposal=proposal,
            registry=registry,
            ledger=ledger,
            portfolio=portfolio,
            submit=lambda request: {"order_id": "OB"},
            persist=persisted.append,
            now=NOW + timedelta(seconds=2),
            max_total_contracts=1,
        )

        self.assertEqual(updated.order_strategies["OB"], "BETA")
        self.assertEqual(len(report["accepted"]), 1)
        self.assertEqual(len(persisted), 2)
        self.assertEqual(persisted[0], registry)

    def test_rejects_broker_position_mismatch_before_submission(self) -> None:
        proposal, registry, ledger, portfolio = setup()
        calls = []
        mismatched = PortfolioSnapshot(portfolio.account, (), ())

        with self.assertRaisesRegex(ValueError, "do not match"):
            submit_beta_orders(
                proposal=proposal,
                registry=registry,
                ledger=ledger,
                portfolio=mismatched,
                submit=calls.append,
                persist=lambda value: None,
                now=NOW,
                max_total_contracts=1,
            )

        self.assertEqual(calls, [])

    def test_unknown_outcome_is_persisted_and_blocks_retry(self) -> None:
        proposal, registry, ledger, portfolio = setup()
        persisted = []

        def timeout(request):
            raise SimTradingUnknownOutcomeError("timeout")

        updated, report = submit_beta_orders(
            proposal=proposal,
            registry=registry,
            ledger=ledger,
            portfolio=portfolio,
            submit=timeout,
            persist=persisted.append,
            now=NOW,
            max_total_contracts=1,
        )

        self.assertEqual(updated.unknown_client_order_ids, ("beta-1",))
        self.assertEqual(len(report["unknown"]), 1)
        self.assertEqual(len(persisted), 2)
        self.assertEqual(persisted[0], registry)

    def test_rejects_stale_hedge_snapshot_before_submission(self) -> None:
        proposal, registry, ledger, portfolio = setup()
        calls = []

        with self.assertRaisesRegex(ValueError, "hedge source market snapshot is stale"):
            submit_beta_orders(
                proposal=proposal,
                registry=registry,
                ledger=ledger,
                portfolio=portfolio,
                submit=calls.append,
                persist=lambda value: None,
                now=NOW + timedelta(seconds=11),
                max_total_contracts=1,
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
