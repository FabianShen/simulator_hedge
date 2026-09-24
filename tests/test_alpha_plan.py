import json
from decimal import Decimal
from pathlib import Path
import unittest

from sim_hedge.execution.alpha.plan import build_plan_from_records, project_alpha_plan


EXAMPLE = Path(__file__).parents[1] / "protocols/pricing/v1/examples/price_request.json"


class AlphaPlanRecordTests(unittest.TestCase):
    def test_builds_serializable_plan_from_recorded_boundaries(self) -> None:
        pricing = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        pricing["underlying"]["previousClose"] = 3.3
        for option in pricing["options"]:
            option["previousSettlement"] = option["marketPrice"]
        put = dict(pricing["options"][2])
        put.update(
            instrument="EXAMPLE-PUT-3.2",
            optionType="OPTION_TYPE_PUT",
            strike=3.2,
        )
        pricing["options"].append(put)
        portfolio = {
            "account": {"account_id": "A1", "cash_balance": "100000"},
            "positions": [],
            "active_orders": [],
        }

        plan = build_plan_from_records(pricing, portfolio)
        output = project_alpha_plan(plan, pricing, account_id="A1")

        self.assertGreaterEqual(plan.margin_capacity, 1)
        self.assertEqual(plan.contracts_per_option, 1)
        self.assertEqual(set(plan.target_positions.values()), {-1})
        self.assertEqual(output["sizing_basis"], "SHORT_OPTION_OPENING_MARGIN")
        self.assertEqual(output["margin_capacity"], plan.margin_capacity)
        self.assertEqual(output["max_contracts_per_option"], 1)
        self.assertFalse(output["orders_generated"])
        json.dumps(output)

    def test_budget_fraction_is_forwarded_to_planner(self) -> None:
        pricing = {
            "requestId": "R1",
            "underlying": {
                "instrument": "ETF",
                "spot": 100,
                "previousClose": 100,
            },
            "options": [
                {
                    "instrument": instrument,
                    "optionType": option_type,
                    "strike": strike,
                    "expiry": "2026-09-23T07:00:00Z",
                    "contractMultiplier": 10000,
                    "priceTick": 0.0001,
                    "marketPrice": 0.1,
                    "previousSettlement": 0.1,
                }
                for instrument, option_type, strike in (
                    ("C110", "OPTION_TYPE_CALL", 110),
                    ("P90", "OPTION_TYPE_PUT", 90),
                )
            ],
        }
        portfolio = {
            "account": {"cash_balance": "1000000"},
            "positions": [],
            "active_orders": [],
        }

        plan = build_plan_from_records(
            pricing, portfolio, budget_fraction=Decimal("0.20")
        )

        self.assertEqual(plan.contracts_per_option, 1)

    def test_refuses_to_initialize_over_existing_positions(self) -> None:
        pricing = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        portfolio = {
            "account": {"cash_balance": "100000"},
            "positions": [{"instrument": "EXISTING"}],
            "active_orders": [],
        }

        with self.assertRaisesRegex(ValueError, "empty broker portfolio"):
            build_plan_from_records(pricing, portfolio)


if __name__ == "__main__":
    unittest.main()
