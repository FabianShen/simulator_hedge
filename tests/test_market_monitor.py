import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sim_hedge.domain import MarketQuote
from sim_hedge.market.monitor import MarketMonitor
from sim_hedge.market.state import MarketState


NOW = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)


class MarketMonitorTests(unittest.TestCase):
    def test_reports_coverage_and_writes_atomic_json(self) -> None:
        state = MarketState(["UNDERLYING"])
        state.apply_quote(MarketQuote(
            instrument="UNDERLYING",
            observed_at=datetime(2026, 9, 17, 10, 30),
            received_at=NOW,
            trading_date=date(2026, 9, 17),
            last=3.34,
            bid=3.339,
            ask=3.340,
        ))
        health = SimpleNamespace(
            state="running",
            data_unsafe=False,
            last_status_error=None,
            last_processing_error=None,
        )
        output = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.json"
            monitor = MarketMonitor(
                state,
                ["UNDERLYING", "OPTION"],
                lambda: health,
                max_age=timedelta(seconds=5),
                json_path=str(path),
                output=output.append,
                clock=lambda: NOW,
            )

            payload = monitor.publish_once()
            written = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["coverage"], {"quoted": 1, "subscribed": 2})
        self.assertTrue(payload["required"]["ready"])
        self.assertEqual(written, payload)
        self.assertIn("coverage=1/2", output[0])

    def test_feed_must_be_running_before_market_is_ready(self) -> None:
        state = MarketState(["UNDERLYING"])
        state.apply_quote(MarketQuote(
            instrument="UNDERLYING",
            observed_at=datetime(2026, 9, 17, 10, 30),
            received_at=NOW,
            trading_date=date(2026, 9, 17),
            last=3.34,
            bid=3.339,
            ask=3.340,
        ))
        health = SimpleNamespace(
            state="created",
            data_unsafe=False,
            last_status_error=None,
            last_processing_error=None,
        )
        output = []
        monitor = MarketMonitor(
            state,
            ["UNDERLYING"],
            lambda: health,
            max_age=timedelta(seconds=5),
            json_path=None,
            output=output.append,
            clock=lambda: NOW,
        )

        payload = monitor.publish_once()

        self.assertFalse(payload["required"]["ready"])
        self.assertFalse(payload["feed"]["active"])
        self.assertIn("feed=CREATED", output[0])


if __name__ == "__main__":
    unittest.main()
