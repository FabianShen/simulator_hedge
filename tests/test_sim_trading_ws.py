import json
from pathlib import Path
import unittest

from sim_hedge.adapters.sim_trading import SimTradingPortfolioSource
from sim_hedge.adapters.sim_trading_ws import SimTradingSnapshotStream
from sim_hedge.portfolio_state import PortfolioState


FIXTURE = Path(__file__).parent / "fixtures" / "sim_trading_snapshot.json"


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self):
        return next(self.messages)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class SimTradingSnapshotStreamTests(unittest.TestCase):
    def test_heartbeat_then_snapshot_replaces_rest_state(self) -> None:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        socket = FakeSocket(
            [json.dumps({"event_type": "HEARTBEAT"}), json.dumps(event)]
        )
        connected = []

        def request(method, url, headers, body):
            if url.endswith("/api/ws/ticket"):
                return {"ticket": "ticket with spaces"}
            return event["payload"]

        def connect(url, timeout):
            connected.append((url, timeout))
            return socket

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="token",
            request_json=request,
        )
        state = PortfolioState()
        state.replace(source.load("ETF-OPTION-1"))
        stream = SimTradingSnapshotStream(
            source,
            "ws://simulator.test/ws/trading",
            "ETF-OPTION-1",
            connect=connect,
        )

        snapshot = stream.replace_from_first_snapshot(state)

        self.assertEqual(state.revision, 2)
        self.assertIs(state.snapshot(), snapshot)
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(
            connected[0][0],
            "ws://simulator.test/ws/trading?ticket=ticket%20with%20spaces",
        )
        self.assertEqual(
            socket.sent[0],
            {"action": "subscribe", "account_ids": ["ETF-OPTION-1"]},
        )
        self.assertEqual(socket.sent[1], {"action": "pong"})
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
