"""Periodic human/JSON projection of in-memory market state."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, Iterable

from sim_hedge.market.state import MarketState


class MarketMonitor:
    """Report market coverage, support platform establishment"""

    def __init__(
        self,
        market_state: MarketState,
        subscribed_instruments: Iterable[str],
        health_provider: Callable[[], Any],
        *,
        max_age: timedelta,
        interval: float = 1.0,
        json_path: str | None = "outputs/market_state.json",
        output: Callable[[str], None] = print,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._market_state = market_state
        self._subscribed = tuple(dict.fromkeys(subscribed_instruments))
        self._health_provider = health_provider
        self._max_age = max_age
        self._interval = interval
        self._json_path = Path(json_path) if json_path else None
        self._output = output
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("monitor already started")
        self._thread = Thread(target=self._run, name="market-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def publish_once(self) -> dict:
        now = self._clock()
        health = self._health_provider()
        quotes = self._market_state.snapshot()
        covered = sum(instrument in quotes for instrument in self._subscribed)
        feed_active = health.state == "running"
        readiness = self._market_state.readiness(
            now=now,
            max_age=self._max_age,
            feed_unsafe=health.data_unsafe or not feed_active,
        )
        payload = {
            "generated_at": now.isoformat(),
            "feed": {
                "state": health.state,
                "active": feed_active,
                "data_unsafe": health.data_unsafe,
                "last_status_error": health.last_status_error,
                "last_processing_error": health.last_processing_error,
            },
            "coverage": {
                "quoted": covered,
                "subscribed": len(self._subscribed),
            },
            "required": {
                "ready": readiness.ready,
                "instruments": list(self._market_state.required_instruments),
                "missing": list(readiness.missing),
                "stale": list(readiness.stale),
            },
            "quotes": {
                instrument: {
                    "observed_at": quote.observed_at.isoformat(),
                    "received_at": quote.received_at.isoformat(),
                    "trading_date": (
                        quote.trading_date.isoformat() if quote.trading_date else None
                    ),
                    "last": quote.last,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "previous_close": quote.previous_close,
                    "previous_settlement": quote.previous_settlement,
                }
                for instrument, quote in sorted(quotes.items())
            },
        }
        if health.data_unsafe:
            feed_label = "UNSAFE"
        elif feed_active:
            feed_label = "SAFE"
        else:
            feed_label = health.state.upper()
        self._output(
            f"market feed={feed_label} coverage={covered}/{len(self._subscribed)} "
            f"required_ready={readiness.ready} missing={len(readiness.missing)} "
            f"stale={len(readiness.stale)}"
        )
        if self._json_path is not None:
            _write_json_atomic(self._json_path, payload)
        return payload

    def _run(self) -> None:
        while not self._stop.is_set():
            self.publish_once()
            self._stop.wait(self._interval)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
