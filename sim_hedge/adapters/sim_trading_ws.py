"""Bootstrap portfolio state from the first simulated-trading WS snapshot."""

from __future__ import annotations

import json
from time import monotonic
from typing import Any, Callable, Mapping
from urllib.parse import quote

import websocket

from hedge_engine import ConfirmedFill
from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
    normalize_confirmed_trade,
    normalize_position,
    normalize_portfolio_snapshot,
)
from sim_hedge.domain.portfolio import PortfolioSnapshot
from sim_hedge.state.portfolio_state import PortfolioState


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

    def watch_positions(
        self,
        state: PortfolioState,
        on_change: Callable[[str, PortfolioSnapshot], None] | None = None,
        *,
        order_strategies: Mapping[str, str] | None = None,
        on_trade: Callable[[ConfirmedFill], None] | None = None,
    ) -> None:
        """Continuously apply absolute position events until stopped or disconnected."""

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
            socket.settimeout(1.0)
            first_business_event = True
            while True:
                try:
                    message = socket.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                event = _event(message)
                event_type = event.get("event_type")
                if event_type == "HEARTBEAT":
                    socket.send(json.dumps({"action": "pong"}))
                    continue
                if event_type in {"ERROR", "AUTH_EXPIRED", "RESYNC_REQUIRED"}:
                    raise SimTradingError(f"WebSocket stream stopped: {event_type}")
                if first_business_event:
                    if event_type != "SNAPSHOT":
                        raise SimTradingError(
                            "first WebSocket business event was not SNAPSHOT"
                        )
                    snapshot = normalize_portfolio_snapshot(event, self._account_id)
                    state.replace(snapshot, synchronized=True)
                    first_business_event = False
                    if on_change is not None:
                        on_change("SNAPSHOT", snapshot)
                    continue
                if event.get("account_id") not in (None, self._account_id):
                    raise SimTradingError("received event for a different account")
                if event_type == "TRADE_CREATED" and on_trade is not None:
                    payload = event.get("payload")
                    if not isinstance(payload, Mapping):
                        raise SimTradingError("TRADE_CREATED payload is not an object")
                    if order_strategies is None:
                        raise SimTradingError("trade callback requires order ownership")
                    on_trade(
                        normalize_confirmed_trade(
                            payload, self._account_id, order_strategies
                        )
                    )
                changed = self._apply_position_event(state, event)
                if changed and on_change is not None:
                    snapshot = state.snapshot()
                    if snapshot is not None:
                        on_change(str(event_type), snapshot)
        finally:
            state.mark_unsynchronized()
            socket.close()

    def _apply_position_event(
        self, state: PortfolioState, event: Mapping[str, Any]
    ) -> bool:
        event_type = event.get("event_type")
        version = event.get("business_version")
        business_version = None if version is None else str(version)
        if event_type == "POSITION_UPDATED":
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise SimTradingError("POSITION_UPDATED payload is not an object")
            position = normalize_position(payload, self._account_id)
            return state.upsert_position(position, business_version)
        if event_type == "POSITION_CLOSED":
            position_id = event.get("entity_id")
            if not position_id:
                raise SimTradingError("POSITION_CLOSED has no entity_id")
            return state.remove_position(str(position_id), business_version)
        return False


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
