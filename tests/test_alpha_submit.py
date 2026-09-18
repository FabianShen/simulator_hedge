from datetime import date, datetime, timedelta, timezone
import unittest

from sim_hedge.adapters.sim_trading import SimTradingUnknownOutcomeError
from sim_hedge.alpha_orders import build_alpha_order_dry_run
from sim_hedge.alpha_submit import submit_alpha_orders
from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


def inputs():
    pricing = {
        "requestId": "R1",
        "asOf": NOW.isoformat(),
        "underlying": {
            "instrument": "ETF",
            "spot": 3.4,
            "observedAt": NOW.isoformat(),
        },
        "options": [
            {
                "instrument": instrument,
                "marketPrice": price,
                "priceTick": "0.0001",
                "observedAt": NOW.isoformat(),
            }
            for instrument, price in (("9001", "0.1"), ("9002", "0.2"))
        ],
    }
    alpha = {
        "plan_id": "alpha-R1",
        "source_pricing_request_id": "R1",
        "account_id": "A1",
        "orders_generated": False,
        "legs": [
            {"instrument": "9001", "quantity": -1},
            {"instrument": "9002", "quantity": -1},
        ],
    }
    proposal, registry = build_alpha_order_dry_run(
        pricing, alpha, exchange_id="SZSE"
    )
    portfolio = PortfolioSnapshot(
        account=AccountSnapshot(
            account_id="A1",
            account_type="ETF_OPTION",
            status="NORMAL",
            trading_day=date(2026, 9, 18),
            risk_state="NORMAL",
        ),
        positions=(),
        active_orders=(),
    )
    return pricing, proposal, registry, portfolio


class AlphaSubmissionTests(unittest.TestCase):
    def test_submits_sequentially_and_persists_every_binding(self) -> None:
        pricing, proposal, registry, portfolio = inputs()
        persisted = []

        updated, report = submit_alpha_orders(
            pricing=pricing,
            proposal=proposal,
            registry=registry,
            portfolio=portfolio,
            confirmed_account_id="A1",
            submit=lambda request: {
                "order_id": f"O-{request['symbol']}"
            },
            persist=persisted.append,
            now=NOW + timedelta(seconds=2),
            max_total_contracts=2,
        )

        self.assertEqual(len(report["accepted"]), 2)
        self.assertEqual(report["rejected"], [])
        self.assertEqual(report["unknown"], [])
        self.assertEqual(len(persisted), 2)
        self.assertEqual(
            updated.order_strategies,
            {"O-9001": "ALPHA", "O-9002": "ALPHA"},
        )

    def test_unknown_outcome_is_persisted_and_stops_later_orders(self) -> None:
        pricing, proposal, registry, portfolio = inputs()
        calls = []
        persisted = []

        def submit(request):
            calls.append(request)
            raise SimTradingUnknownOutcomeError("timeout")

        updated, report = submit_alpha_orders(
            pricing=pricing,
            proposal=proposal,
            registry=registry,
            portfolio=portfolio,
            confirmed_account_id="A1",
            submit=submit,
            persist=persisted.append,
            now=NOW,
            max_total_contracts=2,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(report["unknown"]), 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(
            updated.unknown_client_order_ids,
            (proposal["requests"][0]["client_order_id"],),
        )

    def test_rejects_stale_snapshot_before_any_submission(self) -> None:
        pricing, proposal, registry, portfolio = inputs()
        calls = []

        with self.assertRaisesRegex(ValueError, "stale"):
            submit_alpha_orders(
                pricing=pricing,
                proposal=proposal,
                registry=registry,
                portfolio=portfolio,
                confirmed_account_id="A1",
                submit=calls.append,
                persist=lambda value: None,
                now=NOW + timedelta(seconds=11),
                max_total_contracts=2,
            )

        self.assertEqual(calls, [])

    def test_requires_typed_account_confirmation(self) -> None:
        pricing, proposal, registry, portfolio = inputs()

        with self.assertRaisesRegex(ValueError, "exactly match"):
            submit_alpha_orders(
                pricing=pricing,
                proposal=proposal,
                registry=registry,
                portfolio=portfolio,
                confirmed_account_id="WRONG",
                submit=lambda request: {},
                persist=lambda value: None,
                now=NOW,
                max_total_contracts=2,
            )

    def test_requires_explicit_total_contract_limit(self) -> None:
        pricing, proposal, registry, portfolio = inputs()

        with self.assertRaisesRegex(ValueError, "exceeds explicit limit"):
            submit_alpha_orders(
                pricing=pricing,
                proposal=proposal,
                registry=registry,
                portfolio=portfolio,
                confirmed_account_id="A1",
                submit=lambda request: {},
                persist=lambda value: None,
                now=NOW,
                max_total_contracts=1,
            )


if __name__ == "__main__":
    unittest.main()
