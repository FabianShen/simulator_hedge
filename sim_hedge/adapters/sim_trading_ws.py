"""Bootstrap portfolio state from the first simulated-trading WS snapshot."""

from __future__ import annotations

import json
from time import monotonic
from typing import Any, Callable, Mapping
from urllib.parse import quote

import websocket

from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
    normalize_portfolio_snapshot,
)
from sim_hedge.portfolio import PortfolioSnapshot
from sim_hedge.portfolio_state import PortfolioState


Connect = Callable[..., Any]


class SimTradingSnapshotStream:
    """Apply the documented first WS SNAPSHOT, then close the connection."""

    def __init__(
        self,
        source: SimTradingPortfolioSource,
        ws_url: str,
        account_id: str,
        *,
        timeout: float = 10.0,
        connect: Connect = websocket.create_connection,
    ) -> None:
        if not ws_url or not account_id:
            raise ValueError("ws_url and account_id must not be empty")
        self._source = source
        self._ws_url = ws_url.rstrip("?")
        self._account_id = account_id
        self._timeout = timeout
        self._connect = connect

    def replace_from_first_snapshot(
        self, state: PortfolioState
    ) -> PortfolioSnapshot:
        ticket = quote(self._source.websocket_ticket(), safe="")
        separator = "&" if "?" in self._ws_url else "?"
        socket = self._connect(
            f"{self._ws_url}{separator}ticket={ticket}", timeout=self._timeout
        )
        try:
            socket.send(
                json.dumps(
                    {"action": "subscribe", "account_ids": [self._account_id]}
                )
            )
            deadline = monotonic() + self._timeout
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise SimTradingError("timed out waiting for WebSocket SNAPSHOT")
                socket.settimeout(remaining)
                event = _event(socket.recv())
                event_type = event.get("event_type")
                if event_type == "HEARTBEAT":
                    socket.send(json.dumps({"action": "pong"}))
                    continue
                if event_type in {"ERROR", "AUTH_EXPIRED", "RESYNC_REQUIRED"}:
                    payload = event.get("payload")
                    detail = payload if isinstance(payload, Mapping) else {}
                    message = detail.get("message") or event_type
                    raise SimTradingError(f"WebSocket bootstrap failed: {message}")
                if event_type != "SNAPSHOT":
                    raise SimTradingError(
                        f"expected first WebSocket business event SNAPSHOT, got {event_type!r}"
                    )
                snapshot = normalize_portfolio_snapshot(event, self._account_id)
                state.replace(snapshot)
                return snapshot
        except websocket.WebSocketTimeoutException as exc:
            raise SimTradingError("timed out waiting for WebSocket SNAPSHOT") from exc
        finally:
            socket.close()


def _event(message: Any) -> Mapping[str, Any]:
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    try:
        value = json.loads(message)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimTradingError("WebSocket returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise SimTradingError("WebSocket event is not an object")
    return value
