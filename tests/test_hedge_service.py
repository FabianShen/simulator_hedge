import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf import json_format

from hedge_service import ReferenceHedgeEngine
from hedge_service.protobuf_codec import request_from_proto
from hedging.v1 import hedging_pb2


REQUEST_PATH = (
    Path(__file__).parents[1]
    / "protocols"
    / "hedging"
    / "v1"
    / "examples"
    / "hedge_request.json"
)
RESPONSE_PATH = REQUEST_PATH.with_name("hedge_response.json")
NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class ReferenceHedgeServiceTests(unittest.TestCase):
    def test_protocol_examples_have_matching_identifiers(self) -> None:
        request = hedging_pb2.HedgeRequest()
        response = hedging_pb2.HedgeProposal()
        json_format.Parse(REQUEST_PATH.read_text(encoding="utf-8"), request)
        json_format.Parse(RESPONSE_PATH.read_text(encoding="utf-8"), response)

        self.assertEqual(response.request_id, request.request_id)
        self.assertEqual(
            response.source_pricing_request_id, request.source_pricing_request_id
        )
        self.assertEqual(
            response.base_strategy_ledger_revision,
            request.base_strategy_ledger_revision,
        )
        self.assertFalse(response.orders_generated)

    def test_recorded_request_produces_an_incremental_proposal(self) -> None:
        message = hedging_pb2.HedgeRequest()
        json_format.Parse(REQUEST_PATH.read_text(encoding="utf-8"), message)

        result = ReferenceHedgeEngine().propose(
            request_from_proto(message), created_at=NOW
        )

        self.assertEqual(result.hedge_pair, ("CALL", "PUT"))
        self.assertEqual(
            result.proposal["incremental_trades"], {"CALL": 3, "PUT": 1}
        )
        self.assertEqual(
            result.proposal["target_beta_positions"], {"CALL": 3, "PUT": 1}
        )
        self.assertFalse(result.proposal["orders_generated"])

    def test_missing_held_instrument_greeks_are_rejected(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["instruments"] = [
            item for item in payload["instruments"] if item["instrument"] != "ALPHA"
        ]
        message = hedging_pb2.HedgeRequest()
        json_format.ParseDict(payload, message)

        with self.assertRaisesRegex(ValueError, "held positions.*ALPHA"):
            ReferenceHedgeEngine().propose(
                request_from_proto(message), created_at=NOW
            )


if __name__ == "__main__":
    unittest.main()
