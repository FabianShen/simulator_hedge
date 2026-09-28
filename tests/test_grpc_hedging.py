import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hedge_service.grpc_server import create_server
from hedge_engine.config import HedgeConfig
from hedge_service.protobuf_codec import request_from_proto, response_to_proto
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
    return ReferenceHedgeEngine(
        config=HedgeConfig(
            option_fee=0.0,
            delta_entry_risk_band=0.03,
            delta_target_risk_band=0.0001,
            gamma_entry_risk_band=0.001,
            gamma_target_risk_band=0.00001,
        )
    )


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
        self.assertEqual(proposal["incremental_trades"], {"A": 1, "B": 1, "C": 1})
        self.assertEqual(proposal["target_beta_positions"], {"A": 1, "B": 1, "C": 1})
        self.assertFalse(proposal["orders_generated"])
        self.assertEqual(proposal["decision_policy"], "D_G_MILP")
        diagnostics = proposal["execution_diagnostics"]
        self.assertEqual(len(diagnostics["legs"]), 3)
        self.assertAlmostEqual(
            diagnostics["estimated_transaction_cost"],
            sum(leg["estimated_transaction_cost"] for leg in diagnostics["legs"]),
        )
        self.assertEqual(
            {leg["instrument"] for leg in diagnostics["legs"]}, {"A", "B", "C"}
        )
        self.assertTrue(all(leg["displayed_size"] == 1 for leg in diagnostics["legs"]))
        self.assertGreater(
            diagnostics["estimated_short_margin_before"],
            diagnostics["estimated_short_margin_after"] - 1e-9,
        )

    def test_optional_top_of_book_and_depth_round_trip(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        from google.protobuf import json_format

        message = hedging_pb2.HedgeRequest()
        json_format.ParseDict(payload, message)
        request = request_from_proto(message)

        wing = next(item for item in request.instruments if item.instrument == "A")
        self.assertEqual((wing.bid, wing.ask), (0.1, 0.1))
        self.assertEqual((wing.bid_size, wing.ask_size), (1, 1))

    def test_deprecated_simple_pair_enum_spelling_remains_accepted(self) -> None:
        from google.protobuf import json_format

        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["configuration"]["model"] = "HEDGE_MODEL_SIMPLE_DELTA_GAMMA_PAIR"
        message = hedging_pb2.HedgeRequest()
        json_format.ParseDict(payload, message)

        request = request_from_proto(message)

        self.assertEqual(request.request_id, payload["requestId"])

    def test_recorded_request_and_response_example_match_reference_engine(self) -> None:
        from google.protobuf import json_format

        request_payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        request_message = hedging_pb2.HedgeRequest()
        json_format.ParseDict(request_payload, request_message)
        request = request_from_proto(request_message)
        engine = ReferenceHedgeEngine(
            config=HedgeConfig(
                delta_entry_risk_band=100,
                delta_target_risk_band=10,
                gamma_entry_risk_band=5,
                gamma_target_risk_band=2,
            )
        )
        result = engine.propose(request, created_at=CREATED_AT)
        actual = json_format.MessageToDict(
            response_to_proto(request.request_id, result)
        )
        expected = json.loads(
            (REQUEST_PATH.parent / "hedge_response.json").read_text(encoding="utf-8")
        )

        for field in (
            "incrementalTrades",
            "targetBetaPositions",
            "hedgePair",
            "alphaRisk",
            "portfolioRisk",
            "riskAtTargetBeta",
            "gammaImprovement",
            "decisionPolicy",
            "executionDiagnostics",
        ):
            self.assertEqual(actual[field], expected[field], field)

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
            engine=ReferenceHedgeEngine(),
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
            self.assertEqual(proposal["gamma_improvement"], 0.0)
            self.assertFalse(proposal["orders_generated"])
        finally:
            server.stop(grace=None).wait()

    def test_grpc_routes_in_band_inventory_to_msh_with_execution_diagnostics(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["confirmedAlphaPositions"] = {}
        payload["confirmedBetaPositions"] = {"CALL": "-200"}
        call = next(item for item in payload["instruments"] if item["instrument"] == "CALL")
        call["askSize"] = "1000"

        with GrpcHedgeClient(self.target) as client:
            proposal = client.propose(payload)

        self.assertEqual(proposal["incremental_trades"], {"CALL": 200})
        self.assertEqual(proposal["decision_policy"], "MSH")
        diagnostics = proposal["execution_diagnostics"]
        self.assertAlmostEqual(diagnostics["estimated_short_margin_before"], 992_000.0)
        self.assertAlmostEqual(diagnostics["estimated_short_margin_after"], 0.0)
        self.assertEqual(
            diagnostics["legs"],
            [{
                "instrument": "CALL",
                "side": "BUY",
                "signed_quantity": 200,
                "displayed_size": 1000,
                "displayed_depth_limit": 500,
                "estimated_transaction_cost": 0.0,
                "short_margin_per_contract": 4_960.0,
            }],
        )

if __name__ == "__main__":
    unittest.main()
