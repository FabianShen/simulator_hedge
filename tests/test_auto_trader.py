from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from hedge_engine import (
    ConfirmedFill, OrderIntent, apply_confirmed_fills, bind_broker_order,
    empty_ledger, empty_order_registry, register_order_intent,
)
from sim_hedge.adapters.sim_trading import BrokerOrder, BrokerOrderPage, ConfirmedTradePage
from sim_hedge.auto_trader import _recover_stuck_beta, recover_unsubmitted_alpha, run_once
from sim_hedge.state.registry import registry_from_payload, registry_to_payload
from sim_hedge.domain.portfolio import (
    AccountSnapshot, ActiveOrderSnapshot, PortfolioSnapshot, PositionSnapshot,
)
from sim_hedge.state.ledger import ledger_to_payload


class FakeTradingSource:
    def __init__(self) -> None:
        self.requests = []
        self.cancelled = []
        self.portfolio = PortfolioSnapshot(
            account=AccountSnapshot(
                account_id="A1",
                account_type="ETF_OPTION",
                status="NORMAL",
                trading_day=date(2026, 9, 22),
                risk_state="NORMAL",
                cash_balance=Decimal("1000000"),
            ),
            positions=(),
            active_orders=(),
        )

    def load(self, account_id):
        return self.portfolio

    def submit_etf_option_order(self, request):
        self.requests.append(dict(request))
        return {"order_id": f"O{len(self.requests)}"}

    def cancel_etf_option_order(self, order_id, account_id):
        self.cancelled.append((order_id, account_id))
        return {"order_id": order_id, "status": "CANCELLED"}

    def load_order_page(self, account_id, trading_day, **kwargs):
        return BrokerOrderPage((), None, False)

    def load_confirmed_trade_page(self, account_id, strategies, **kwargs):
        return ConfirmedTradePage((), None, False)


class AutoTraderTests(unittest.TestCase):
    def test_stuck_beta_is_cancelled_only_after_timeout(self) -> None:
        now = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
        source = FakeTradingSource()
        registry = register_order_intent(
            empty_order_registry("A1"),
            OrderIntent(
                "beta-1", "A1", "BETA", "SZSE", "C34", 1,
                "OPEN", "LIMIT", Decimal("0.1"), now - timedelta(seconds=180),
                proposal_id="hedge-1",
            ),
        )
        registry = bind_broker_order(
            registry, client_order_id="beta-1", order_id="O1"
        )
        order = BrokerOrder(
            "O1", "beta-1", "A1", "SZSE", "C34", "BUY", "OPEN",
            "LIMIT", Decimal("0.1"), 1, 0, 1, 0, "ACCEPTED",
            now - timedelta(seconds=180),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "registry.json"
            at_boundary = _recover_stuck_beta(
                source=source, registry=registry, ledger=empty_ledger("A1"),
                orders=(order,), cancel_after=180, now=now,
                registry_path=registry_path,
            )
            after_boundary = _recover_stuck_beta(
                source=source, registry=registry, ledger=empty_ledger("A1"),
                orders=(order,), cancel_after=180,
                now=now + timedelta(microseconds=1), registry_path=registry_path,
            )

        self.assertIsNone(at_boundary)
        self.assertEqual(after_boundary, "CANCELLED stuck Beta order(s): O1")
        self.assertEqual(source.cancelled, [("O1", "A1")])

    def test_cancelled_beta_retirement_is_idempotent(self) -> None:
        now = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
        source = FakeTradingSource()
        registry = register_order_intent(
            empty_order_registry("A1"),
            OrderIntent(
                "beta-1", "A1", "BETA", "SZSE", "C34", 1,
                "OPEN", "LIMIT", Decimal("0.1"), now - timedelta(minutes=5),
                proposal_id="hedge-1",
            ),
        )
        registry = bind_broker_order(
            registry, client_order_id="beta-1", order_id="O1"
        )
        order = BrokerOrder(
            "O1", "beta-1", "A1", "SZSE", "C34", "BUY", "OPEN",
            "LIMIT", Decimal("0.1"), 1, 0, 0, 1, "CANCELLED",
            now - timedelta(minutes=5),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "registry.json"
            first = _recover_stuck_beta(
                source=source, registry=registry, ledger=empty_ledger("A1"),
                orders=(order,), cancel_after=180, now=now,
                registry_path=registry_path,
            )
            saved = registry_from_payload(
                json.loads(registry_path.read_text(encoding="utf-8"))
            )
            original = registry_path.read_bytes()
            second = _recover_stuck_beta(
                source=source, registry=saved, ledger=empty_ledger("A1"),
                orders=(order,), cancel_after=180, now=now,
                registry_path=registry_path,
            )

            self.assertEqual(registry_path.read_bytes(), original)

        self.assertEqual(first, "RETIRED cancelled Beta order(s): O1")
        self.assertIsNone(second)
        self.assertEqual(saved.retired_cancelled_client_order_ids, ("beta-1",))
        self.assertEqual(source.cancelled, [])

    def test_continues_other_alpha_leg_while_first_order_is_working(self) -> None:
        now = datetime.now(timezone.utc)
        source = FakeTradingSource()
        position = PositionSnapshot(
            "P1", "A1", "C34", "SHORT", Decimal(1), Decimal(1),
            Decimal(0), Decimal(0), Decimal(1),
        )
        active = ActiveOrderSnapshot(
            "O1", "A1", "C34", "PARTIALLY_FILLED", "SELL",
            Decimal(2), Decimal(1), Decimal(1),
        )
        source.portfolio = PortfolioSnapshot(source.portfolio.account, (position,), (active,))
        registry = empty_order_registry("A1")
        for client_id, instrument in (("alpha-1", "C34"), ("alpha-2", "P32")):
            registry = register_order_intent(
                registry,
                OrderIntent(
                    client_id, "A1", "ALPHA", "SZSE", instrument, -2,
                    "OPEN", "COUNTERPARTY", None, now,
                ),
            )
        registry = bind_broker_order(registry, client_order_id="alpha-1", order_id="O1")
        fill = ConfirmedFill("T1", "O1", "A1", "ALPHA", "C34", -1, Decimal("0.1"), now)
        ledger = apply_confirmed_fills(empty_ledger("A1"), (fill,))
        order = BrokerOrder(
            "O1", "alpha-1", "A1", "SZSE", "C34", "SELL", "OPEN",
            "COUNTERPARTY", Decimal("0.1"), 2, 1, 1, 0, "PARTIALLY_FILLED", now,
        )
        source.load_order_page = lambda *args, **kwargs: BrokerOrderPage((order,), None, False)
        source.load_confirmed_trade_page = lambda *args, **kwargs: ConfirmedTradePage((fill,), None, False)
        def submit(request):
            source.requests.append(dict(request))
            return {"order_id": "O2"}
        source.submit_etf_option_order = submit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "auto"
            output_dir.mkdir()
            (output_dir / "alpha_plan.json").write_text(json.dumps({
                "plan_id": "P1", "account_id": "A1", "contracts_per_option": 2,
                "legs": [
                    {"instrument": "C34", "quantity": -2},
                    {"instrument": "P32", "quantity": -2},
                ],
            }), encoding="utf-8")
            (root / "alpha_market.json").write_text(json.dumps({
                "requestId": "M2", "asOf": now.isoformat(),
                "tradingDate": "2026-09-22",
                "options": [{"instrument": "P32", "bid": None, "ask": None}],
            }), encoding="utf-8")
            (root / "registry.json").write_text(json.dumps(registry_to_payload(registry)), encoding="utf-8")
            (root / "ledger.json").write_text(json.dumps(ledger_to_payload(ledger)), encoding="utf-8")

            status = run_once(
                source=source, account_id="A1", exchange_id="SZSE",
                alpha_market_path=root / "alpha_market.json",
                risk_path=root / "risk.json", registry_path=root / "registry.json",
                ledger_path=root / "ledger.json", output_dir=output_dir,
                budget_fraction=Decimal("0.30"), max_alpha_contracts=4,
                max_beta_contracts=10, max_snapshot_age=10,
            )
            saved = json.loads((root / "registry.json").read_text(encoding="utf-8"))

        self.assertEqual(status, "SUBMITTED ALPHA: 1 continuation order")
        self.assertEqual(len(source.requests), 1)
        self.assertEqual(source.requests[0]["symbol"], "P32")
        self.assertEqual(saved["broker_orders"]["O1"], "alpha-1")
        self.assertEqual(saved["broker_orders"]["O2"], "alpha-2")
        self.assertEqual(len(saved["broker_orders"]), 2)

    def test_stale_market_does_not_save_order_intents(self) -> None:
        source = FakeTradingSource()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market = {
                "requestId": "M1",
                "asOf": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "tradingDate": "2026-09-22",
            }
            (root / "alpha_market.json").write_text(json.dumps(market), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale"):
                run_once(
                    source=source, account_id="A1", exchange_id="SZSE",
                    alpha_market_path=root / "alpha_market.json",
                    risk_path=root / "risk.json", registry_path=root / "registry.json",
                    ledger_path=root / "ledger.json", output_dir=root / "auto",
                    budget_fraction=Decimal("0.30"), max_alpha_contracts=100,
                    max_beta_contracts=10, max_snapshot_age=10,
                )
            self.assertFalse((root / "registry.json").exists())
            self.assertEqual(source.requests, [])

    def test_recovery_abandons_only_after_empty_broker_check(self) -> None:
        source = FakeTradingSource()
        registry = register_order_intent(
            empty_order_registry("A1"),
            OrderIntent(
                "alpha-old", "A1", "ALPHA", "SZSE", "C34", -2,
                "OPEN", "COUNTERPARTY", None,
                datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            ledger_path = root / "ledger.json"
            registry_path.write_text(json.dumps(registry_to_payload(registry)), encoding="utf-8")
            ledger_path.write_text(json.dumps(ledger_to_payload(empty_ledger("A1"))), encoding="utf-8")
            count = recover_unsubmitted_alpha(
                source=source, account_id="A1",
                registry_path=registry_path, ledger_path=ledger_path,
            )
            saved = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual(saved["abandoned_client_order_ids"], ["alpha-old"])
            self.assertEqual(source.requests, [])

    def test_recovery_preserves_registry_when_broker_history_is_not_empty(self) -> None:
        source = FakeTradingSource()
        source.load_order_page = lambda *args, **kwargs: BrokerOrderPage((object(),), None, False)
        registry = register_order_intent(
            empty_order_registry("A1"),
            OrderIntent(
                "alpha-old", "A1", "ALPHA", "SZSE", "C34", -2,
                "OPEN", "COUNTERPARTY", None,
                datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            ledger_path = root / "ledger.json"
            registry_path.write_text(json.dumps(registry_to_payload(registry)), encoding="utf-8")
            ledger_path.write_text(json.dumps(ledger_to_payload(empty_ledger("A1"))), encoding="utf-8")
            original = registry_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "order history is not empty"):
                recover_unsubmitted_alpha(
                    source=source, account_id="A1",
                    registry_path=registry_path, ledger_path=ledger_path,
                )
            self.assertEqual(registry_path.read_bytes(), original)

    def test_initializes_margin_sized_alpha_without_manual_confirmation(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        market = {
            "requestId": "M1",
            "asOf": now,
            "tradingDate": "2026-09-22",
            "underlying": {
                "instrument": "ETF",
                "spot": 3.3,
                "previousClose": 3.3,
            },
            "options": [
                {
                    "instrument": instrument,
                    "optionType": option_type,
                    "strike": strike,
                    "expiry": "2026-09-23T00:00:00Z",
                    "contractMultiplier": 10000,
                    "priceTick": 0.0001,
                    "marketPrice": 0.1,
                    "previousSettlement": 0.1,
                }
                for instrument, option_type, strike in (
                    ("C34", "OPTION_TYPE_CALL", 3.4),
                    ("P32", "OPTION_TYPE_PUT", 3.2),
                )
            ],
        }
        source = FakeTradingSource()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha_market = root / "alpha_market.json"
            alpha_market.write_text(json.dumps(market), encoding="utf-8")
            status = run_once(
                source=source,
                account_id="A1",
                exchange_id="SZSE",
                alpha_market_path=alpha_market,
                risk_path=root / "risk.json",
                registry_path=root / "registry.json",
                ledger_path=root / "ledger.json",
                output_dir=root / "auto",
                budget_fraction=Decimal("0.30"),
                max_alpha_contracts=100,
                max_beta_contracts=10,
                max_snapshot_age=10,
            )
            plan = json.loads((root / "auto" / "alpha_plan.json").read_text())
            registry = json.loads((root / "registry.json").read_text())

        self.assertEqual(status, "SUBMITTED ALPHA: 2 orders")
        self.assertEqual(len(source.requests), 2)
        self.assertTrue(
            all(request["order_type"] == "COUNTERPARTY" for request in source.requests)
        )
        self.assertEqual(plan["sizing_basis"], "SHORT_OPTION_OPENING_MARGIN")
        self.assertEqual(plan["contracts_per_option"], 37)
        self.assertEqual(len(registry["broker_orders"]), 2)


if __name__ == "__main__":
    unittest.main()
