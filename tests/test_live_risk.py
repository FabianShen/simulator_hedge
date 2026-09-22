import json
import tempfile
import unittest
from pathlib import Path

from sim_hedge.live_risk import write_live_risk_snapshot


class LiveRiskPublicationTests(unittest.TestCase):
    def test_atomically_writes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_risk.json"
            write_live_risk_snapshot(path, {"status": "READY"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "READY"})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
