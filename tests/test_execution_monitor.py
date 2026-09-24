import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timezone

from execution_engine import ExecutionAssessment
from hedge_engine import build_hedge_proposal, empty_ledger
from sim_hedge.state.monitor import (
    assessment_to_payload,
    build_accepted_execution_batch,
    proposal_from_accepted_batch,
    _write_once,
)


def proposal(request_id="pricing-1"):
    return build_hedge_proposal(
        pricing_request_id=request_id,
        account_id="A1",
        base_ledger_revision=0,
        created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        engine_name="test",
        engine_version="1",
        confirmed_beta_positions={},
        incremental_trades={"CALL": 1},
    )


class ExecutionMonitorTests(unittest.TestCase):
    def test_accepts_valid_proposal_as_non_trading_batch(self) -> None:
        value = proposal()
        accepted = build_accepted_execution_batch(value, empty_ledger("A1"))

        self.assertFalse(accepted["submission_allowed"])
        restored = proposal_from_accepted_batch(accepted)
        self.assertEqual(restored["proposal_id"], value["proposal_id"])
        self.assertEqual(restored["incremental_trades"], value["incremental_trades"])

    def test_rejects_acceptance_against_different_ledger(self) -> None:
        with self.assertRaisesRegex(ValueError, "revisions"):
            build_accepted_execution_batch(
                proposal(),
                replace(empty_ledger("A1"), revision=1),
            )

    def test_accepted_file_is_idempotent_but_cannot_be_replaced(self) -> None:
        first = build_accepted_execution_batch(proposal(), empty_ledger("A1"))
        second = build_accepted_execution_batch(
            proposal("pricing-2"), empty_ledger("A1")
        )
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "accepted.json")
            self.assertTrue(_write_once(path, first))
            self.assertFalse(_write_once(path, first))
            with self.assertRaisesRegex(ValueError, "different contents"):
                _write_once(path, second)
            self.assertEqual(json.loads(Path(path).read_text()), first)

    def test_status_payload_never_grants_trading_permission(self) -> None:
        payload = assessment_to_payload(
            ExecutionAssessment(
                proposal_id="hedge-1",
                status="READY_TO_SUBMIT",
                requested_trades={"CALL": 1},
                confirmed_fills={},
                working_trades={},
                uncovered_trades={"CALL": 1},
                cancellation_candidate_ids=(),
                blockers=(),
            )
        )

        self.assertFalse(payload["submission_allowed"])
        self.assertEqual(payload["broker_operations_performed"], 0)


if __name__ == "__main__":
    unittest.main()
