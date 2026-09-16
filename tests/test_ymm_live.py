import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from sim_hedge.adapters.ymm_live import LiveMarketDataError, YmmLiveDataSource, normalize_tick
from sim_hedge.domain import MarketQuote


class FakeSdk:
    def __init__(self) -> None:
        self.init_calls = []
        self.closed = False

    def init(self, *, token: str, mode: str) -> None:
        self.init_calls.append((token, mode))

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, batch) -> None:
        self.batch = batch
        self.channels = []
        self.status_handler = None
        self.closed = False

    def listen_status(self, handler) -> None:
        self.status_handler = handler

    def subscribe(self, channels) -> None:
        self.channels = list(channels)

    def listen(self, *, tick_handler) -> None:
        tick_handler(self.batch)

    def close(self) -> None:
        self.closed = True


class NormalizeTickTests(unittest.TestCase):
    def test_normalizes_a_live_tick(self) -> None:
        observed = datetime(2026, 9, 16, 10, 30)
        received = datetime(2026, 9, 16, 2, 30, 0, 1000, tzinfo=timezone.utc)

        quote = normalize_tick(
            {
                "order_book_id": "159915.XSHE",
                "datetime": observed,
                "trading_date": date(2026, 9, 16),
                "last": 2.031,
                "bid": [2.030, 2.029],
                "ask": [2.032, 2.033],
            },
            received_at=received,
        )

        self.assertEqual(
            quote,
            MarketQuote(
                "159915.XSHE", observed, received, date(2026, 9, 16),
                2.031, 2.030, 2.032,
            ),
        )

    def test_rejects_a_tick_without_any_valid_price(self) -> None:
        with self.assertRaisesRegex(LiveMarketDataError, "at least one valid price"):
            normalize_tick(
                {
                    "order_book_id": "159915.XSHE",
                    "datetime": datetime(2026, 9, 16, 10, 30),
                    "last": None,
                    "bid": [0],
                    "ask": [],
                }
            )


class YmmLiveDataSourceTests(unittest.TestCase):
    def test_subscribes_and_delivers_quotes_off_the_sdk_callback(self) -> None:
        sdk = FakeSdk()
        client = FakeClient(({
            "order_book_id": "159915.XSHE",
            "datetime": datetime(2026, 9, 16, 10, 30),
            "trading_date": date(2026, 9, 16),
            "last": 2.031,
            "bid": [2.030],
            "ask": [2.032],
        },))
        received = []
        source = YmmLiveDataSource(
            token="secret",
            instruments=["159915.XSHE"],
            sdk=sdk,
            client_factory=lambda: client,
        )

        source.run(received.append)

        self.assertEqual(sdk.init_calls, [("secret", "lan")])
        self.assertEqual(client.channels, ["tick_159915.XSHE"])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].instrument, "159915.XSHE")
        self.assertEqual(source.health.received_messages, 1)
        self.assertEqual(source.health.state, "stopped")
        self.assertTrue(client.closed)
        self.assertTrue(sdk.closed)

    def test_marks_disconnect_status_as_unsafe(self) -> None:
        source = YmmLiveDataSource(
            token="secret",
            instruments=["159915.XSHE"],
            sdk=FakeSdk(),
            client_factory=lambda: FakeClient(()),
        )

        source._on_status(SimpleNamespace(component="hub", state="disconnected"))

        self.assertTrue(source.health.data_unsafe)
        self.assertEqual(source.health.last_error, "hub/disconnected")

    def test_closes_sdk_when_client_creation_fails(self) -> None:
        sdk = FakeSdk()

        def fail_to_connect():
            raise RuntimeError("token already in use")

        source = YmmLiveDataSource(
            token="secret",
            instruments=["159915.XSHE"],
            sdk=sdk,
            client_factory=fail_to_connect,
        )

        with self.assertRaisesRegex(RuntimeError, "token already in use"):
            source.run(lambda quote: None)

        self.assertTrue(sdk.closed)
        self.assertEqual(source.health.state, "stopped")
        self.assertTrue(source.health.data_unsafe)


if __name__ == "__main__":
    unittest.main()
