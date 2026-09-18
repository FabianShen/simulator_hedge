import time
import unittest
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

from sim_hedge.pricing_types import PricingBatch, SabrFit
from sim_hedge.pricing_worker import ContinuousPricingWorker


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


class FakePricingClient:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.completed = Event()
        self._lock = Lock()
        self.active = 0
        self.maximum_active = 0

    def health(self):
        raise NotImplementedError

    def price(self, request):
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return _batch(request["requestId"])
        finally:
            with self._lock:
                self.active -= 1
            self.completed.set()

    def close(self) -> None:
        pass


class ContinuousPricingWorkerTests(unittest.TestCase):
    def test_coalesces_updates_and_keeps_one_call_in_flight(self) -> None:
        client = FakePricingClient(delay=0.02)
        results = []
        worker = ContinuousPricingWorker(
            client,
            lambda request_id, as_of: _request(request_id, as_of),
            interval=0.01,
            on_result=results.append,
            clock=lambda: NOW,
        )
        worker.start()
        try:
            for _ in range(20):
                worker.request_update()
            self.assertTrue(client.completed.wait(1.0))
        finally:
            worker.stop()

        self.assertEqual(client.maximum_active, 1)
        self.assertEqual(worker.health.successes, 1)
        self.assertIsNotNone(worker.latest())
        self.assertEqual(len(results), 1)

    def test_rejects_response_for_stale_request_snapshot(self) -> None:
        client = FakePricingClient()
        worker = ContinuousPricingWorker(
            client,
            lambda request_id, as_of: _request(
                request_id,
                as_of - timedelta(seconds=3),
            ),
            interval=0.01,
            max_result_age=timedelta(seconds=2),
            clock=lambda: NOW,
        )
        worker.start()
        try:
            worker.request_update()
            self.assertTrue(client.completed.wait(1.0))
        finally:
            worker.stop()

        self.assertEqual(worker.health.stale_responses, 1)
        self.assertIsNone(worker.latest())


def _request(request_id: str, as_of: datetime) -> dict:
    return {
        "requestId": request_id,
        "asOf": as_of.isoformat().replace("+00:00", "Z"),
    }


def _batch(request_id: str) -> PricingBatch:
    return PricingBatch(
        request_id=request_id,
        calculated_at=NOW,
        engine_name="fake",
        engine_version="1",
        model="SABR_BLACK_76",
        calibration=SabrFit(
            forward=3.3,
            alpha=0.3,
            beta=0.5,
            nu=0.8,
            rho=-0.3,
            rmse=0.001,
            valid_strikes=3,
        ),
        results=(),
    )


if __name__ == "__main__":
    unittest.main()
