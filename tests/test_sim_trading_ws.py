import json
from copy import deepcopy
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
        message = next(self.messages)
        if isinstance(message, BaseException):
            raise message
        return message

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

    def test_watch_applies_absolute_position_events_and_becomes_unsafe(self) -> None:
        snapshot_event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        update = deepcopy(snapshot_event["payload"]["accounts"][0]["positions"][0])
        update["position"]["today_volume"] = "2"
        update["position"]["available_volume"] = "3"
        socket = FakeSocket(
            [
                json.dumps(snapshot_event),
                json.dumps(
                    {
                        "event_type": "POSITION_UPDATED",
                        "account_id": "ETF-OPTION-1",
                        "entity_id": "P-1",
                        "business_version": "12346",
                        "payload": update,
                    }
                ),
                json.dumps(
                    {
                        "event_type": "POSITION_CLOSED",
                        "account_id": "ETF-OPTION-1",
                        "entity_id": "P-1",
                        "business_version": "12347",
                        "payload": {},
                    }
                ),
                KeyboardInterrupt(),
            ]
        )

        def request(method, url, headers, body):
            if url.endswith("/api/ws/ticket"):
                return {"ticket": "one-use"}
            return snapshot_event["payload"]

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="token",
            request_json=request,
        )
        state = PortfolioState()
        state.replace(source.load("ETF-OPTION-1"))
        events = []
        stream = SimTradingSnapshotStream(
            source,
            "ws://simulator.test/ws/trading",
            "ETF-OPTION-1",
            connect=lambda url, timeout: socket,
        )

        with self.assertRaises(KeyboardInterrupt):
            stream.watch_positions(
                state, lambda event, snapshot: events.append(event)
            )

        self.assertEqual(events, ["SNAPSHOT", "POSITION_UPDATED", "POSITION_CLOSED"])
        self.assertEqual(state.snapshot().positions, ())
        self.assertEqual(state.snapshot().business_version, "12347")
        self.assertFalse(state.synchronized)
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
