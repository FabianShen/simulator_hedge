import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pricing_engine import SabrPricingEngine
from pricing_engine.__main__ import load_request
from pricing_engine.grpc_server import create_server
from sim_hedge.adapters.grpc_pricing import (
    GrpcPricingClient,
    PricingServiceError,
)


REQUEST_PATH = (
    Path(__file__).parents[1]
    / "protocols"
    / "pricing"
    / "v1"
    / "examples"
    / "price_request.json"
)
CALCULATED_AT = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)


class GrpcPricingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, port = create_server(
            "127.0.0.1:0",
            clock=lambda: CALCULATED_AT,
        )
        cls.server.start()
        cls.target = f"127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop(grace=None).wait()

    def test_health_reports_compatible_protocol(self) -> None:
        with GrpcPricingClient(self.target) as client:
            health = client.health()

        self.assertEqual(health.protocol_version, "pricing.v1")
        self.assertEqual(health.engine_name, "reference-python-sabr")
        self.assertEqual(health.engine_version, "0.2.0")

    def test_grpc_matches_direct_engine(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        direct = SabrPricingEngine().price(load_request(REQUEST_PATH))

        with GrpcPricingClient(self.target) as client:
            remote = client.price(payload)

        self.assertEqual(remote.request_id, payload["requestId"])
        self.assertEqual(remote.calculated_at, CALCULATED_AT)
        self.assertEqual(remote.calibration.valid_strikes, 3)
        self.assertAlmostEqual(
            remote.calibration.rmse,
            direct.calibration.rmse,
            places=15,
        )
        direct_by_instrument = {
            result.instrument: result for result in direct.results
        }
        for remote_result in remote.results:
            direct_result = direct_by_instrument[remote_result.instrument]
            self.assertAlmostEqual(
                remote_result.theoretical_price,
                direct_result.theoretical_price,
                places=15,
            )
            self.assertAlmostEqual(
                remote_result.delta,
                direct_result.delta,
                places=15,
            )

    def test_grpc_values_option_without_market_price(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        extra = dict(payload["options"][0])
        extra["instrument"] = "HELD-FAR"
        extra["strike"] = 3.9
        extra.pop("marketPrice", None)
        extra.pop("marketPriceSource", None)
        payload["options"].append(extra)
        with GrpcPricingClient(self.target) as client:
            response = client.price(payload)
        held = next(item for item in response.results if item.instrument == "HELD-FAR")
        self.assertEqual(held.status, "OK")
        self.assertIsNone(held.market_implied_volatility)
        self.assertIsNotNone(held.delta)
        self.assertEqual(response.calibration.valid_strikes, 3)

    def test_invalid_request_returns_invalid_argument(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["configuration"]["sabr"]["minimumStrikes"] = 20

        with GrpcPricingClient(self.target) as client:
            with self.assertRaisesRegex(PricingServiceError, "INVALID_ARGUMENT"):
                client.price(payload)

    def test_stopped_service_fails_safely(self) -> None:
        server, port = create_server("127.0.0.1:0")
        server.start()
        server.stop(grace=None).wait()

        with GrpcPricingClient(f"127.0.0.1:{port}", timeout=0.05) as client:
            with self.assertRaisesRegex(
                PricingServiceError,
                "UNAVAILABLE|DEADLINE_EXCEEDED",
            ):
                client.health()

    def test_health_rejects_protocol_mismatch(self) -> None:
        server, port = create_server(
            "127.0.0.1:0",
            protocol_version="pricing.v999",
        )
        server.start()
        try:
            with GrpcPricingClient(f"127.0.0.1:{port}") as client:
                with self.assertRaisesRegex(PricingServiceError, "protocol mismatch"):
                    client.health()
        finally:
            server.stop(grace=None).wait()

    def test_deadline_prevents_slow_engine_from_blocking_client(self) -> None:
        class SlowEngine:
            def price(self, request):
                time.sleep(0.1)
                return SabrPricingEngine().price(request)

        server, port = create_server("127.0.0.1:0", engine=SlowEngine())
        server.start()
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        try:
            with GrpcPricingClient(f"127.0.0.1:{port}", timeout=0.01) as client:
                with self.assertRaisesRegex(PricingServiceError, "DEADLINE_EXCEEDED"):
                    client.price(payload)
        finally:
            server.stop(grace=None).wait()


if __name__ == "__main__":
    unittest.main()
