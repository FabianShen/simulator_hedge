"""Live FeedHub adapter backed by ``ymm_live_data_sdk``."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import import_module
import math
from queue import Full, Queue
from threading import Thread
from typing import Any, Callable

from sim_hedge.domain.quote import MarketQuote


class LiveMarketDataError(RuntimeError):
    """A live message cannot be normalized safely."""


@dataclass
class LiveFeedHealth:
    state: str = "created"
    received_batches: int = 0
    received_messages: int = 0
    dropped_batches: int = 0
    rejected_messages: int = 0
    data_unsafe: bool = False
    last_status_error: str | None = None
    last_processing_error: str | None = None


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
        self._stop_requested = False
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
        except KeyboardInterrupt:
            self._stop_requested = True
            raise
        except Exception as exc:
            self.health.data_unsafe = True
            self.health.last_status_error = f"{type(exc).__name__}: {exc}"
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

        self._stop_requested = True
        if self._client is not None:
            self._client.close()

    def _on_ticks(self, batch: tuple[dict, ...]) -> None:
        """SDK callback: enqueue the immutable batch and return immediately."""

        try:
            received_at = datetime.now(timezone.utc)
            self._queue.put_nowait((received_at, batch))
            self.health.received_batches += 1
            self.health.received_messages += len(batch)
        except Full:
            self.health.dropped_batches += 1
            self.health.data_unsafe = True
            self.health.last_status_error = "strategy queue overflowed"

    def _on_status(self, event: Any) -> None:
        component = getattr(event, "component", "")
        state = getattr(event, "state", "")

        # closing SDK are expected
        if self._stop_requested and (
            (component == "hub" and state in {"disconnected", "reconnecting"})
            or (component == "session" and state == "closed")
        ):
            return
        
        unsafe = (
            # Loss connection
            (component == "hub" and state in {"disconnected", "reconnecting"})
            or 
            # Processing failures
            (
                component == "session"
                and state in {
                    "slow_consumer",
                    "subscription_isolated",
                    "handler_failed",
                    "closed",
                }
            )
            # Server failure
            or (component == "catalog" and state == "subscription_inactive")
        )

        if unsafe:
            self.health.data_unsafe = True
            self.health.last_status_error = f"{component}/{state}"

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                received_at, batch = item
                for message in batch:
                    try:
                        quote = normalize_tick(message, received_at=received_at)
                        self._on_quote(quote)
                    except Exception as exc:
                        self.health.rejected_messages += 1
                        self.health.data_unsafe = True
                        self.health.last_processing_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()


def normalize_tick(message: dict[str, Any], received_at: datetime | None = None) -> MarketQuote:
    """Convert one FeedHub tick dictionary into the internal quote type."""

    instrument = str(message.get("order_book_id") or "")
    observed_at = _parse_datetime(message.get("datetime"))
    trading_date = _parse_date(message.get("trading_date"))
    if not instrument:
        raise LiveMarketDataError("tick has no order_book_id")
    if observed_at is None:
        raise LiveMarketDataError(f"tick for {instrument} has no valid datetime")
    if message.get("trading_date") is not None and trading_date is None:
        raise LiveMarketDataError(f"tick for {instrument} has no valid trading_date")

    try:
        bid, bid_size = _price_and_size(message, "bid", "bid_vol", instrument)
        ask, ask_size = _price_and_size(message, "ask", "ask_vol", instrument)
    except ValueError as exc:
        if isinstance(exc, LiveMarketDataError):
            raise
        raise LiveMarketDataError(f"invalid depth for {instrument}: {exc}") from exc

    try:
        return MarketQuote(
            instrument=instrument,
            observed_at=observed_at,
            received_at=received_at or datetime.now(timezone.utc),
            trading_date=trading_date,
            last=_positive_float(message.get("last")),
            bid=bid,
            ask=ask,
            previous_close=_positive_float(message.get("prev_close")),
            previous_settlement=_positive_float(message.get("prev_settlement")),
            bid_size=bid_size,
            ask_size=ask_size,
        )
    except ValueError as exc:
        raise LiveMarketDataError(f"invalid tick for {instrument}: {exc}") from exc


def _price_and_size(
    message: dict[str, Any], price_key: str, size_key: str, instrument: str
) -> tuple[float | None, int | None]:
    prices = message.get(price_key)
    sizes = message.get(size_key)
    if prices is None:
        if sizes is not None:
            raise LiveMarketDataError(
                f"{size_key} has no matching {price_key} prices for {instrument}"
            )
        return None, None
    if prices is not None and sizes is not None:
        try:
            price_count, size_count = len(prices), len(sizes)
        except TypeError as exc:
            raise LiveMarketDataError(
                f"{price_key} and {size_key} must be sequences for {instrument}"
            ) from exc
        if price_count != size_count:
            raise LiveMarketDataError(
                f"{price_key} and {size_key} depth lengths differ for {instrument}"
            )
    price = _first_positive(prices)
    size = _first_nonnegative_integer(sizes, size_key)
    return price, size


def _first_nonnegative_integer(values: Any, field: str) -> int | None:
    if values is None:
        return None
    try:
        value = values[0]
    except (IndexError, KeyError, TypeError):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must contain nonnegative integer values")
    try:
        number = int(value)
        if float(value) != number or number < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must contain nonnegative integer values") from exc
    return number


def _first_positive(values: Any) -> float | None:
    if values is None:
        return None
    try:
        return _positive_float(values[0])
    except (IndexError, KeyError, TypeError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, str)):
        text = str(value).strip()
        fmt = "%Y%m%d%H%M%S" if len(text) == 14 else "%Y%m%d%H%M%S%f"
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, str)):
        try:
            return datetime.strptime(str(value).strip(), "%Y%m%d").date()
        except ValueError:
            pass
    return None


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
