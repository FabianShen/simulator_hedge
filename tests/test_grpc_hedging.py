import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hedge_service.grpc_server import create_server
from sim_hedge.adapters.grpc_hedging import GrpcHedgeClient, HedgeServiceError


REQUEST_PATH = (
    Path(__file__).parents[1]
    / "protocols"
    / "hedging"
    / "v1"
    / "examples"
    / "hedge_request.json"
)
CREATED_AT = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class GrpcHedgingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, port = create_server(
            "127.0.0.1:0", clock=lambda: CREATED_AT
        )
        cls.server.start()
        cls.target = f"127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop(grace=None).wait()

    def test_health_reports_both_compatible_protocols(self) -> None:
        with GrpcHedgeClient(self.target) as client:
            health = client.health()

        self.assertEqual(health["protocol_version"], "hedging.v1")
        self.assertEqual(
            health["proposal_protocol_version"], "sim-hedge/hedge-proposal/v1"
        )
        self.assertEqual(health["engine_name"], "reference-python-hedge")

    def test_grpc_returns_the_versioned_incremental_proposal(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))

        with GrpcHedgeClient(self.target) as client:
            proposal = client.propose(payload)

        self.assertEqual(proposal["created_at"], CREATED_AT.isoformat())
        self.assertEqual(proposal["base_strategy_ledger_revision"], 6)
        self.assertEqual(proposal["confirmed_beta_positions"], {})
        self.assertEqual(proposal["incremental_trades"], {"CALL": 3, "PUT": 1})
        self.assertEqual(proposal["target_beta_positions"], {"CALL": 3, "PUT": 1})
        self.assertFalse(proposal["orders_generated"])

    def test_invalid_request_returns_invalid_argument(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["spot"] = 0

        with GrpcHedgeClient(self.target) as client:
            with self.assertRaisesRegex(HedgeServiceError, "INVALID_ARGUMENT"):
                client.propose(payload)

    def test_health_rejects_protocol_mismatch(self) -> None:
        server, port = create_server(
            "127.0.0.1:0", protocol_version="hedging.v999"
        )
        server.start()
        try:
            with GrpcHedgeClient(f"127.0.0.1:{port}") as client:
                with self.assertRaisesRegex(HedgeServiceError, "protocol mismatch"):
                    client.health()
        finally:
            server.stop(grace=None).wait()

    def test_stopped_service_fails_safely(self) -> None:
        server, port = create_server("127.0.0.1:0")
        server.start()
        server.stop(grace=None).wait()

        with GrpcHedgeClient(f"127.0.0.1:{port}", timeout=0.05) as client:
            with self.assertRaisesRegex(
                HedgeServiceError, "UNAVAILABLE|DEADLINE_EXCEEDED"
            ):
                client.health()


if __name__ == "__main__":
    unittest.main()
