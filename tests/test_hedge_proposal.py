import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hedge_engine import (
    build_hedge_proposal,
    validate_hedge_proposal,
)


NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class HedgeProposalContractTests(unittest.TestCase):
    def test_documented_example_is_a_valid_proposal(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "protocols"
            / "hedging"
            / "v1"
            / "examples"
            / "hedge_proposal.json"
        )
        proposal = json.loads(path.read_text(encoding="utf-8"))

        trades = validate_hedge_proposal(
            proposal,
            pricing_request_id="pricing-20260921T030000Z-7",
            account_id="ETO_EXAMPLE",
            base_ledger_revision=6,
            confirmed_beta_positions={"90007074": 1, "90007084": 1},
        )

        self.assertEqual(trades, {"90007074": -1, "90007076": 1})

    def test_binds_incremental_trades_to_confirmed_beta_and_revision(self) -> None:
        proposal = build_hedge_proposal(
            pricing_request_id="pricing-1",
            account_id="A1",
            base_ledger_revision=4,
            created_at=NOW,
            engine_name="test-engine",
            engine_version="1",
            confirmed_beta_positions={"OLD": 2},
            incremental_trades={"OLD": -2, "NEW": 1},
        )

        self.assertEqual(proposal["confirmed_beta_positions"], {"OLD": 2})
        self.assertEqual(proposal["incremental_trades"], {"NEW": 1, "OLD": -2})
        self.assertEqual(proposal["target_beta_positions"], {"NEW": 1})
        self.assertEqual(
            validate_hedge_proposal(
                proposal,
                pricing_request_id="pricing-1",
                account_id="A1",
                base_ledger_revision=4,
                confirmed_beta_positions={"OLD": 2},
            ),
            {"NEW": 1, "OLD": -2},
        )

    def test_proposal_id_is_deterministic(self) -> None:
        arguments = dict(
            pricing_request_id="pricing-1",
            account_id="A1",
            base_ledger_revision=4,
            created_at=NOW,
            engine_name="test-engine",
            engine_version="1",
            confirmed_beta_positions={},
            incremental_trades={"CALL": 1},
        )

        self.assertEqual(
            build_hedge_proposal(**arguments)["proposal_id"],
            build_hedge_proposal(**arguments)["proposal_id"],
        )

    def test_rejects_a_target_changed_outside_the_incremental_trade(self) -> None:
        proposal = build_hedge_proposal(
            pricing_request_id="pricing-1",
            account_id="A1",
            base_ledger_revision=4,
            created_at=NOW,
            engine_name="test-engine",
            engine_version="1",
            confirmed_beta_positions={},
            incremental_trades={"CALL": 1},
        )
        proposal["target_beta_positions"] = {"CALL": 2}

        with self.assertRaisesRegex(ValueError, "target Beta positions"):
            validate_hedge_proposal(
                proposal,
                pricing_request_id="pricing-1",
                account_id="A1",
                base_ledger_revision=4,
                confirmed_beta_positions={},
            )

    def test_rejects_a_different_confirmed_beta_base(self) -> None:
        proposal = build_hedge_proposal(
            pricing_request_id="pricing-1",
            account_id="A1",
            base_ledger_revision=4,
            created_at=NOW,
            engine_name="test-engine",
            engine_version="1",
            confirmed_beta_positions={"CALL": 1},
            incremental_trades={"CALL": 1},
        )

        with self.assertRaisesRegex(ValueError, "confirmed Beta"):
            validate_hedge_proposal(
                proposal,
                pricing_request_id="pricing-1",
                account_id="A1",
                base_ledger_revision=4,
                confirmed_beta_positions={"CALL": 2},
            )


if __name__ == "__main__":
    unittest.main()
