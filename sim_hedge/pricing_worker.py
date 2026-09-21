"""Non-blocking coordinator for continuous external pricing."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from sim_hedge.ports import PricingClient
from sim_hedge.pricing_request import PricingRequestError
from sim_hedge.pricing_types import PricingBatch


@dataclass(frozen=True)
class PricingWorkerHealth:
    state: str
    requests_sent: int
    successes: int
    failures: int
    skipped_inputs: int
    stale_responses: int
    last_request_id: str | None
    last_error: str | None


class ContinuousPricingWorker:
    """Coalesce quote updates and keep at most one pricing RPC in flight."""

    def __init__(
        self,
        client: PricingClient,
        request_factory: Callable[[str, datetime], Mapping[str, Any]],
        *,
        interval: float = 1.0,
        max_result_age: timedelta = timedelta(seconds=2),
        on_result: Callable[[PricingBatch], None] | None = None,
        on_priced: Callable[[Mapping[str, Any], PricingBatch], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if max_result_age <= timedelta(0):
            raise ValueError("max_result_age must be positive")
        self._client = client
        self._request_factory = request_factory
        self._interval = interval
        self._max_result_age = max_result_age
        self._on_result = on_result
        self._on_priced = on_priced
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock
        self._wake = Event()
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._latest: PricingBatch | None = None
        self._state = "created"
        self._requests_sent = 0
        self._successes = 0
        self._failures = 0
        self._skipped_inputs = 0
        self._stale_responses = 0
        self._last_request_id: str | None = None
        self._last_error: str | None = None
        self._sequence = 0

    @property
    def health(self) -> PricingWorkerHealth:
        with self._lock:
            return PricingWorkerHealth(
                state=self._state,
                requests_sent=self._requests_sent,
                successes=self._successes,
                failures=self._failures,
                skipped_inputs=self._skipped_inputs,
                stale_responses=self._stale_responses,
                last_request_id=self._last_request_id,
                last_error=self._last_error,
            )

    def latest(self) -> PricingBatch | None:
        with self._lock:
            return self._latest

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("pricing worker already started")
        self._thread = Thread(
            target=self._run,
            name="continuous-pricing",
            daemon=True,
        )
        self._thread.start()

    def request_update(self) -> None:
        """Signal new market data without waiting for pricing."""

        if not self._stop.is_set():
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        with self._lock:
            self._state = "running"
        next_allowed = self._monotonic()
        while True:
            self._wake.wait()
            if self._stop.is_set():
                break
            remaining = next_allowed - self._monotonic()
            if remaining > 0 and self._stop.wait(remaining):
                break
            # Updates received before this point are coalesced into one request.
            self._wake.clear()
            self._execute_once()
            next_allowed = self._monotonic() + self._interval
        with self._lock:
            self._state = "stopped"

    def _execute_once(self) -> None:
        as_of = self._clock()
        if as_of.tzinfo is None:
            self._record_failure("pricing worker clock must be timezone-aware")
            return
        as_of_utc = as_of.astimezone(timezone.utc)
        with self._lock:
            self._sequence += 1
            request_id = (
                f"pricing-{as_of_utc.strftime('%Y%m%dT%H%M%S.%fZ')}"
                f"-{self._sequence}"
            )
        try:
            request = self._request_factory(request_id, as_of)
        except PricingRequestError as exc:
            with self._lock:
                self._skipped_inputs += 1
                self._last_error = str(exc)
            return
        except Exception as exc:
            self._record_failure(
                f"request factory failed: {type(exc).__name__}: {exc}"
            )
            return

        with self._lock:
            self._requests_sent += 1
            self._last_request_id = request_id
        try:
            response = self._client.price(request)
        except Exception as exc:
            self._record_failure(f"pricing call failed: {type(exc).__name__}: {exc}")
            return
        if response.request_id != request_id:
            self._record_failure("pricing response request_id does not match")
            return

        try:
            request_time = _parse_protocol_time(request.get("asOf"))
        except (TypeError, ValueError) as exc:
            self._record_failure(f"invalid request timestamp: {exc}")
            return
        if self._clock().astimezone(timezone.utc) - request_time > self._max_result_age:
            with self._lock:
                self._stale_responses += 1
                self._last_error = "pricing response arrived after its freshness limit"
            return

        with self._lock:
            self._latest = response
            self._successes += 1
            self._last_error = None
        if self._on_result is not None:
            try:
                self._on_result(response)
            except Exception as exc:
                self._record_failure(
                    f"pricing result callback failed: {type(exc).__name__}: {exc}"
                )
        if self._on_priced is not None:
            try:
                self._on_priced(request, response)
            except Exception as exc:
                self._record_failure(
                    f"priced snapshot callback failed: {type(exc).__name__}: {exc}"
                )

    def _record_failure(self, error: str) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = error


def _parse_protocol_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("pricing request asOf must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("pricing request asOf must be timezone-aware")
    return parsed.astimezone(timezone.utc)
