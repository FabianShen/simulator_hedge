import unittest
from datetime import datetime, timezone

from execution_engine import (
    ConfirmedExecutionFill,
    WorkingExecutionOrder,
    assess_execution,
    start_execution_batch,
)
from hedge_engine import build_hedge_proposal


class ExecutionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = build_hedge_proposal(
            pricing_request_id="pricing-1",
            account_id="A1",
            base_ledger_revision=7,
            created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            engine_name="test",
            engine_version="1",
            confirmed_beta_positions={"OLD": 1},
            incremental_trades={"CALL": 3, "PUT": -2},
        )
        self.batch = start_execution_batch(
            self.proposal,
            pricing_request_id="pricing-1",
            account_id="A1",
            ledger_revision=7,
            confirmed_beta_positions={"OLD": 1},
        )

    def test_new_incremental_proposal_is_ready(self) -> None:
        result = assess_execution(self.batch)

        self.assertEqual(result.status, "READY_TO_SUBMIT")
        self.assertEqual(result.uncovered_trades, {"CALL": 3, "PUT": -2})

    def test_working_remainder_prevents_duplicate_submission(self) -> None:
        result = assess_execution(
            self.batch,
            confirmed_fills=(
                ConfirmedExecutionFill("F1", self.batch.proposal_id, "CALL", 1),
            ),
            working_orders=(
                WorkingExecutionOrder("C1", self.batch.proposal_id, "CALL", 2),
                WorkingExecutionOrder("C2", self.batch.proposal_id, "PUT", -2),
            ),
        )

        self.assertEqual(result.status, "WORKING")
        self.assertEqual(result.uncovered_trades, {})

    def test_cancelled_remainder_becomes_uncovered(self) -> None:
        result = assess_execution(
            self.batch,
            confirmed_fills=(
                ConfirmedExecutionFill("F1", self.batch.proposal_id, "CALL", 1),
                ConfirmedExecutionFill("F2", self.batch.proposal_id, "PUT", -2),
            ),
        )

        self.assertEqual(result.status, "READY_TO_SUBMIT")
        self.assertEqual(result.uncovered_trades, {"CALL": 2})

    def test_all_confirmed_fills_complete_the_increment(self) -> None:
        result = assess_execution(
            self.batch,
            confirmed_fills=(
                ConfirmedExecutionFill("F1", self.batch.proposal_id, "CALL", 3),
                ConfirmedExecutionFill("F2", self.batch.proposal_id, "PUT", -2),
            ),
        )

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.uncovered_trades, {})

    def test_prior_proposal_working_order_blocks_new_signal(self) -> None:
        result = assess_execution(
            self.batch,
            working_orders=(
                WorkingExecutionOrder("OLD-C1", "hedge-older", "CALL", 1),
            ),
        )

        self.assertEqual(result.status, "BLOCKED_BY_PRIOR_PROPOSAL")
        self.assertEqual(result.uncovered_trades, {})
        self.assertEqual(result.cancellation_candidate_ids, ("OLD-C1",))

    def test_rejects_progress_in_the_wrong_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "wrong direction"):
            assess_execution(
                self.batch,
                confirmed_fills=(
                    ConfirmedExecutionFill(
                        "F1", self.batch.proposal_id, "PUT", 1
                    ),
                ),
            )

    def test_rejects_overfill(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds requested"):
            assess_execution(
                self.batch,
                confirmed_fills=(
                    ConfirmedExecutionFill(
                        "F1", self.batch.proposal_id, "CALL", 4
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
