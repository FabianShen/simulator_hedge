import json
from pathlib import Path
import unittest

from sim_hedge.execution_replay import replay_execution_scenario


class ExecutionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "protocols"
            / "execution"
            / "v1"
            / "examples"
            / "lifecycle.json"
        )
        self.scenario = json.loads(path.read_text(encoding="utf-8"))

    def test_recorded_lifecycle_reaches_complete_without_broker_operations(self) -> None:
        result = replay_execution_scenario(self.scenario)

        self.assertEqual(
            [frame["status"] for frame in result["frames"]],
            [
                "BLOCKED_BY_PRIOR_PROPOSAL",
                "READY_TO_SUBMIT",
                "WORKING",
                "WORKING",
                "READY_TO_SUBMIT",
                "WORKING",
                "COMPLETE",
            ],
        )
        self.assertEqual(result["broker_operations_performed"], 0)

    def test_replay_fails_when_recorded_expectation_is_wrong(self) -> None:
        self.scenario["frames"][1]["expected_status"] = "COMPLETE"

        with self.assertRaisesRegex(ValueError, "expected COMPLETE"):
            replay_execution_scenario(self.scenario)


if __name__ == "__main__":
    unittest.main()
