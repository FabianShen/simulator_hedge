"""Live FeedHub adapter backed by ``ymm_live_data_sdk``."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import import_module
import math
from queue import Full, Queue
from threading import Thread
from typing import Any, Callable

from sim_hedge.domain import MarketQuote


class LiveMarketDataError(RuntimeError):
    """A live message cannot be normalized safely."""


@dataclass
class LiveFeedHealth:
    state: str = "created"
    received_batches: int = 0
    received_messages: int = 0
    dropped_batches: int = 0
    data_unsafe: bool = False
    last_error: str | None = None


class YmmLiveDataSource:
    """Subscribe to live ticks and deliver normalized quotes off the SDK thread."""

    _STOP = object()

    def __init__(
        self,
        token: str,
        instruments: list[str],
        mode: str = "lan",
        queue_size: int = 1_000,
        sdk: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not token:
            raise ValueError("token must not be empty")
        if not instruments or any(not code for code in instruments):
            raise ValueError("at least one non-empty instrument is required")
        if mode not in {"lan", "TS"}:
            raise ValueError("mode must be 'lan' or 'TS'")
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")

        self._token = token
        self._instruments = list(dict.fromkeys(instruments))
        self._mode = mode
        self._sdk = sdk or import_module("ymm_live_data_sdk")
        self._client_factory = client_factory
        self._queue: Queue[Any] = Queue(maxsize=queue_size)
        self._client: Any | None = None
        self._worker: Thread | None = None
        self._on_quote: Callable[[MarketQuote], None] | None = None
        self.health = LiveFeedHealth()

    @property
    def channels(self) -> list[str]:
        return [f"tick_{instrument}" for instrument in self._instruments]

    def run(self, on_quote: Callable[[MarketQuote], None]) -> None:
        """Connect and block until the client is stopped or the SDK exits."""

        self._on_quote = on_quote
        self.health.state = "connecting"
        try:
            self._sdk.init(token=self._token, mode=self._mode)
            self._client = (
                self._client_factory()
                if self._client_factory is not None
                else self._sdk.LiveMarketDataClient()
            )
            self._worker = Thread(target=self._consume, name="live-quote-consumer")
            self._worker.start()
            self._client.listen_status(self._on_status)
            self._client.subscribe(self.channels)
            self.health.state = "running"
            self._client.listen(tick_handler=self._on_ticks)
        except Exception as exc:
            self.health.data_unsafe = True
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.health.state = "stopping"
            if self._worker is not None:
                self._queue.put(self._STOP)
                self._queue.join()
                self._worker.join()
            if self._client is not None:
                self._client.close()
            self._sdk.close()
            self.health.state = "stopped"

    def stop(self) -> None:
        """Cause the SDK's blocking ``listen`` call to return."""

        if self._client is not None:
            self._client.close()

    def _on_ticks(self, batch: tuple[dict, ...]) -> None:
        """SDK callback: enqueue the immutable batch and return immediately."""

        try:
            self._queue.put_nowait(batch)
            self.health.received_batches += 1
            self.health.received_messages += len(batch)
        except Full:
            self.health.dropped_batches += 1
            self.health.data_unsafe = True
            self.health.last_error = "strategy queue overflowed"

    def _on_status(self, event: Any) -> None:
        component = getattr(event, "component", "")
        state = getattr(event, "state", "")
        unsafe = (
            (component == "hub" and state in {"disconnected", "reconnecting"})
            or (
                component == "session"
                and state in {
                    "slow_consumer",
                    "subscription_isolated",
                    "handler_failed",
                    "closed",
                }
            )
            or (component == "catalog" and state == "subscription_inactive")
        )
        if unsafe:
            self.health.data_unsafe = True
            self.health.last_error = f"{component}/{state}"

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                for message in item:
                    try:
                        quote = normalize_tick(message)
                        self._on_quote(quote)
                    except Exception as exc:
                        self.health.data_unsafe = True
                        self.health.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()


def normalize_tick(message: dict[str, Any], received_at: datetime | None = None) -> MarketQuote:
    """Convert one FeedHub tick dictionary into the internal quote type."""

    instrument = str(message.get("order_book_id") or "")
    observed_at = message.get("datetime")
    trading_date = message.get("trading_date")
    if not instrument:
        raise LiveMarketDataError("tick has no order_book_id")
    if not isinstance(observed_at, datetime):
        raise LiveMarketDataError(f"tick for {instrument} has no valid datetime")
    if trading_date is not None and not isinstance(trading_date, date):
        raise LiveMarketDataError(f"tick for {instrument} has no valid trading_date")

    try:
        return MarketQuote(
            instrument=instrument,
            observed_at=observed_at,
            received_at=received_at or datetime.now(timezone.utc),
            trading_date=trading_date,
            last=_positive_float(message.get("last")),
            bid=_first_positive(message.get("bid")),
            ask=_first_positive(message.get("ask")),
        )
    except ValueError as exc:
        raise LiveMarketDataError(f"invalid tick for {instrument}: {exc}") from exc


def _first_positive(values: Any) -> float | None:
    if values is None:
        return None
    try:
        return _positive_float(values[0])
    except (IndexError, KeyError, TypeError):
        return None


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
