import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hedge_service.grpc_server import create_server
from hedging.v1 import hedging_pb2
from sim_hedge.adapters.grpc_hedging import GrpcHedgeClient, HedgeServiceError
from sim_hedge.adapters.grpc_hedging import _proposal_from_proto
from hedge_service import ReferenceHedgeEngine

REQUEST_PATH = (
    Path(__file__).parents[1]
    / "protocols"
    / "hedging"
    / "v1"
    / "examples"
    / "hedge_request.json"
)
CREATED_AT = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


def configured_engine() -> ReferenceHedgeEngine:
    return ReferenceHedgeEngine(delta_limit=0.0, gamma_limit=0.0)


class GrpcHedgingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, port = create_server(
            "127.0.0.1:0", engine=configured_engine(), clock=lambda: CREATED_AT
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
        self.assertEqual(
            proposal["source_market_as_of"], "2026-09-21T03:00:00+00:00"
        )
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
            "127.0.0.1:0", engine=configured_engine(),
            protocol_version="hedging.v999"
        )
        server.start()
        try:
            with GrpcHedgeClient(f"127.0.0.1:{port}") as client:
                with self.assertRaisesRegex(HedgeServiceError, "protocol mismatch"):
                    client.health()
        finally:
            server.stop(grace=None).wait()

    def test_stopped_service_fails_safely(self) -> None:
        server, port = create_server(
            "127.0.0.1:0", engine=configured_engine()
        )
        server.start()
        server.stop(grace=None).wait()

        with GrpcHedgeClient(f"127.0.0.1:{port}", timeout=0.05) as client:
            with self.assertRaisesRegex(
                HedgeServiceError, "UNAVAILABLE|DEADLINE_EXCEEDED"
            ):
                client.health()

    def test_response_without_source_market_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(HedgeServiceError, "source_market_as_of"):
            _proposal_from_proto(hedging_pb2.HedgeProposal())
            
    def test_grpc_preserves_no_trade_decision(self) -> None:
        server, port = create_server(
            "127.0.0.1:0",
            engine=ReferenceHedgeEngine(
                delta_limit=1e9,
                gamma_limit=1e9,
            ),
            clock=lambda: CREATED_AT,
        )
        server.start()
        try:
            payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))

            with GrpcHedgeClient(f"127.0.0.1:{port}") as client:
                proposal = client.propose(payload)

            self.assertEqual(proposal["incremental_trades"], {})
            self.assertEqual(proposal["hedge_pair"], [])
            self.assertEqual(
                proposal["target_beta_positions"],
                proposal["confirmed_beta_positions"],
            )
            self.assertEqual(proposal["normalized_residual"], 0.0)
            self.assertFalse(proposal["orders_generated"])
        finally:
            server.stop(grace=None).wait()

if __name__ == "__main__":
    unittest.main()
