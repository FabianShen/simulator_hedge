from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from sim_hedge.auto_trader import run_once
from sim_hedge.portfolio import AccountSnapshot, PortfolioSnapshot


class FakeTradingSource:
    def __init__(self) -> None:
        self.requests = []
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


class AutoTraderTests(unittest.TestCase):
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
