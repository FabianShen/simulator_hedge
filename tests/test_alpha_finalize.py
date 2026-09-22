from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from hedge_engine import (
    ConfirmedFill, OrderIntent, apply_confirmed_fills, bind_broker_order,
    empty_ledger, empty_order_registry, mark_submission_unknown,
    register_order_intent,
)
from sim_hedge.alpha_finalize import finalize_alpha, validate_alpha_adoption
from sim_hedge.adapters.sim_trading import BrokerOrder, BrokerOrderPage, ConfirmedTradePage
from sim_hedge.auto_trader import run_once
from sim_hedge.order_registry import registry_to_payload
from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot, PositionSnapshot
from sim_hedge.strategy_ledger import ledger_to_payload


def case():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    registry = empty_order_registry("A1")
    for client, instrument in (("alpha-filled", "C1"), ("alpha-unsubmitted", "P1")):
        registry = register_order_intent(registry, OrderIntent(
            client, "A1", "ALPHA", "SZSE", instrument, -2,
            "OPEN", "COUNTERPARTY", None, now,
        ))
    registry = bind_broker_order(registry, client_order_id="alpha-filled", order_id="O1")
    ledger = apply_confirmed_fills(empty_ledger("A1"), (
        ConfirmedFill("T1", "O1", "A1", "ALPHA", "C1", -2, Decimal("0.1"), now),
    ))
    plan = {"plan_id": "P", "account_id": "A1", "legs": [
        {"instrument": "C1", "quantity": -2},
        {"instrument": "P1", "quantity": -2},
    ]}
    report = {
        "account_id": "A1", "registry_revision": registry.revision,
        "ledger_revision": ledger.revision, "position_match": True,
        "account_healthy": True, "broker_positions": {"C1": -2},
        "active_order_ids": [], "unresolved_submissions": [],
        "unbound_order_intents": ["alpha-unsubmitted"],
        "managed_order_statuses": {"O1": "FILLED"},
    }
    return plan, registry, ledger, report


class AlphaFinalizeTests(unittest.TestCase):
    def test_adopts_only_filled_holdings(self):
        plan, registry, ledger, report = case()
        adoption, updated = finalize_alpha(plan, registry, ledger, report)
        self.assertEqual(adoption["alpha_positions"], {"C1": -2})
        self.assertEqual(adoption["abandoned_client_order_ids"], ["alpha-unsubmitted"])
        self.assertEqual(updated.abandoned_client_order_ids, ("alpha-unsubmitted",))
        self.assertEqual(ledger.alpha_positions, {"C1": -2})
        validate_alpha_adoption(adoption, plan, updated, ledger)
        with self.assertRaisesRegex(ValueError, "newer registry"):
            validate_alpha_adoption(adoption, plan, registry, ledger)

    def test_unknown_submission_blocks_without_abandoning(self):
        plan, registry, ledger, report = case()
        registry = mark_submission_unknown(registry, "alpha-unsubmitted")
        report["registry_revision"] = registry.revision
        report["unresolved_submissions"] = ["alpha-unsubmitted"]
        with self.assertRaisesRegex(ValueError, "unknown submission"):
            finalize_alpha(plan, registry, ledger, report)
        self.assertEqual(registry.abandoned_client_order_ids, ())

    def test_working_or_unfilled_broker_order_blocks(self):
        plan, registry, ledger, report = case()
        report["managed_order_statuses"] = {"O1": "PARTIALLY_FILLED"}
        with self.assertRaisesRegex(ValueError, "not fully filled"):
            finalize_alpha(plan, registry, ledger, report)
        report["managed_order_statuses"] = {"O1": "FILLED"}
        report["active_order_ids"] = ["O2"]
        with self.assertRaisesRegex(ValueError, "active orders"):
            finalize_alpha(plan, registry, ledger, report)

    def test_auto_trader_skips_old_alpha_target_after_adoption(self):
        plan, registry, ledger, report = case()
        adoption, registry = finalize_alpha(plan, registry, ledger, report)
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        fill = next(iter(ledger.applied_trades.values()))
        order = BrokerOrder(
            "O1", "alpha-filled", "A1", "SZSE", "C1", "SELL", "OPEN",
            "COUNTERPARTY", Decimal("0.1"), 2, 2, 0, 0, "FILLED", now,
        )

        class Source:
            def __init__(self):
                self.requests = []

            def load(self, account_id):
                return PortfolioSnapshot(
                    AccountSnapshot(
                        "A1", "ETF_OPTION", "NORMAL", now.date(),
                        "NORMAL", Decimal("1000000"),
                    ),
                    (PositionSnapshot(
                        "P1", "A1", "C1", "SHORT", Decimal(2), Decimal(2),
                        Decimal(0), Decimal(0), Decimal(1),
                    ),), (),
                )

            def load_order_page(self, *args, **kwargs):
                return BrokerOrderPage((order,), None, False)

            def load_confirmed_trade_page(self, *args, **kwargs):
                return ConfirmedTradePage((fill,), None, False)

            def submit_etf_option_order(self, request):
                self.requests.append(request)
                raise AssertionError("Alpha top-up was submitted")

        source = Source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "auto"
            output.mkdir()
            (output / "alpha_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (output / "alpha_adoption.json").write_text(json.dumps(adoption), encoding="utf-8")
            (root / "registry.json").write_text(json.dumps(registry_to_payload(registry)), encoding="utf-8")
            (root / "ledger.json").write_text(json.dumps(ledger_to_payload(ledger)), encoding="utf-8")
            status = run_once(
                source=source, account_id="A1", exchange_id="SZSE",
                alpha_market_path=root / "missing_market.json",
                risk_path=root / "missing_risk.json",
                registry_path=root / "registry.json", ledger_path=root / "ledger.json",
                output_dir=output, budget_fraction=Decimal("0.30"),
                max_alpha_contracts=10, max_beta_contracts=10,
                max_snapshot_age=10,
            )
        self.assertEqual(status, "WAITING: no live hedge proposal")
        self.assertEqual(source.requests, [])


if __name__ == "__main__":
    unittest.main()
