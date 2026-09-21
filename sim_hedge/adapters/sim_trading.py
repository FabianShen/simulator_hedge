"""Read-only adapter for the simulated-trading REST API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.parse import urlencode
import http.cookiejar

from hedge_engine import ConfirmedFill
from sim_hedge.portfolio import (
    AccountSnapshot,
    ActiveOrderSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)


class SimTradingError(RuntimeError):
    pass


class SimTradingUnknownOutcomeError(SimTradingError):
    """The request may have reached the simulator and must not be blindly retried."""


JsonRequest = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None], Any]


@dataclass(frozen=True)
class ConfirmedTradePage:
    fills: tuple[ConfirmedFill, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    client_order_id: str
    account_id: str
    exchange_id: str
    instrument: str
    direction: str
    offset: str
    order_type: str
    limit_price: Decimal | None
    total_volume: int
    traded_volume: int
    remaining_volume: int
    cancelled_volume: int
    status: str
    created_at: datetime


@dataclass(frozen=True)
class BrokerOrderPage:
    orders: tuple[BrokerOrder, ...]
    next_cursor: str | None
    has_more: bool


class SimTradingPortfolioSource:
    """Authenticated adapter for simulated-trading broker facts and submission."""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        timeout: float = 10.0,
        request_json: JsonRequest | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._timeout = timeout
        self._opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._request_json = request_json or self._http_json

    def login(self, username: str, password: str) -> None:
        if not username or not password:
            raise ValueError("username and password must not be empty")
        payload = self._request_json(
            "POST",
            f"{self._base_url}/api/auth/login",
            {"Content-Type": "application/json"},
            {"username": username, "password": password},
        )
        data = _unwrap(payload)
        token = data.get("access_token") if isinstance(data, Mapping) else None
        if not token:
            raise SimTradingError("login response did not contain access_token")
        self._access_token = str(token)

    def list_accounts(self) -> tuple[Mapping[str, Any], ...]:
        payload = _unwrap(self._get("/api/accounts"))
        if isinstance(payload, Mapping):
            payload = payload.get("items") or payload.get("accounts") or []
        if not isinstance(payload, list):
            raise SimTradingError("accounts response is not a list")
        return tuple(item for item in payload if isinstance(item, Mapping))

    def load(self, account_id: str) -> PortfolioSnapshot:
        if not account_id:
            raise ValueError("account_id must not be empty")
        raw = self._get(f"/api/accounts/{account_id}/trading-snapshot")
        return normalize_portfolio_snapshot(raw, account_id)

    def settlement_account(self, option_account_id: str) -> Mapping[str, Any]:
        payload = _unwrap(
            self._get(
                f"/api/etf-options/accounts/{option_account_id}/settlement-account"
            )
        )
        if not isinstance(payload, Mapping):
            raise SimTradingError("settlement-account response is not an object")
        return payload

    def load_confirmed_trade_page(
        self,
        account_id: str,
        order_strategies: Mapping[str, str],
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ConfirmedTradePage:
        """Read one authoritative trade page and attach known strategy ownership."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        if limit <= 0 or limit > 100:
            raise ValueError("trade page limit must be between 1 and 100")
        query: dict[str, Any] = {"account_id": account_id, "limit": limit}
        if cursor:
            query["cursor"] = cursor
        raw = self._get(f"/api/trades/page?{urlencode(query)}")
        return normalize_confirmed_trade_page(raw, account_id, order_strategies)

    def load_order_page(
        self,
        account_id: str,
        trading_day: date,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BrokerOrderPage:
        """Read one authoritative order page for recovery and audit."""

        if not account_id:
            raise ValueError("account_id must not be empty")
        if limit <= 0 or limit > 100:
            raise ValueError("order page limit must be between 1 and 100")
        query: dict[str, Any] = {
            "account_id": account_id,
            "trading_day": trading_day.isoformat(),
            "limit": limit,
        }
        if cursor:
            query["cursor"] = cursor
        raw = self._get(f"/api/orders/page?{urlencode(query)}")
        return normalize_order_page(raw, account_id)

    def load_order(self, order_id: str, account_id: str) -> BrokerOrder:
        if not order_id:
            raise ValueError("order_id must not be empty")
        return normalize_order(self._get(f"/api/orders/{order_id}"), account_id)

    def submit_etf_option_order(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Submit one already-validated ETF-option request."""

        if not self._access_token:
            raise SimTradingError("not authenticated; provide a token or call login()")
        try:
            payload = _unwrap(
                self._request_json(
                    "POST",
                    f"{self._base_url}/api/etf-options/orders",
                    {
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/json",
                    },
                    request,
                )
            )
        except (TimeoutError, URLError) as exc:
            raise SimTradingUnknownOutcomeError(
                f"ETF-option submission outcome is unknown: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SimTradingUnknownOutcomeError(
                "ETF-option order response is not an object; outcome is unknown"
            )
        if not payload.get("order_id"):
            raise SimTradingUnknownOutcomeError(
                "ETF-option order response has no order_id; outcome is unknown"
            )
        return payload

    def websocket_ticket(self) -> str:
        """Create the short-lived, single-use ticket required by the WS API."""

        if not self._access_token:
            raise SimTradingError("not authenticated; provide a token or call login()")
        payload = _unwrap(
            self._request_json(
                "POST",
                f"{self._base_url}/api/ws/ticket",
                {"Authorization": f"Bearer {self._access_token}"},
                None,
            )
        )
        ticket = payload.get("ticket") if isinstance(payload, Mapping) else None
        if not ticket:
            raise SimTradingError("ticket response did not contain ticket")
        return str(ticket)

    def _get(self, path: str) -> Any:
        if not self._access_token:
            raise SimTradingError("not authenticated; provide a token or call login()")
        return self._request_json(
            "GET",
            f"{self._base_url}{path}",
            {"Authorization": f"Bearer {self._access_token}"},
            None,
        )

    def _http_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Any:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(url, data=encoded, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if (
                method == "POST"
                and url.endswith("/api/etf-options/orders")
                and exc.code >= 500
            ):
                raise SimTradingUnknownOutcomeError(
                    f"simulator HTTP {exc.code}; submission outcome may be unknown: {detail}"
                ) from exc
            raise SimTradingError(f"simulator HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            if method == "POST" and url.endswith("/api/etf-options/orders"):
                raise SimTradingUnknownOutcomeError(
                    f"simulator connection failed; outcome may be unknown: {exc}"
                ) from exc
            raise SimTradingError(f"simulator connection failed: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if method == "POST" and url.endswith("/api/etf-options/orders"):
                raise SimTradingUnknownOutcomeError(
                    "simulator returned invalid JSON; submission outcome may be unknown"
                ) from exc
            raise SimTradingError("simulator returned invalid JSON") from exc


def normalize_portfolio_snapshot(raw: Any, account_id: str) -> PortfolioSnapshot:
    """Normalize REST or WebSocket SNAPSHOT layouts into one stable model."""

    payload = _unwrap(raw)
    if not isinstance(payload, Mapping):
        raise SimTradingError("trading snapshot is not an object")
    if payload.get("event_type") == "SNAPSHOT" and isinstance(
        payload.get("payload"), Mapping
    ):
        envelope = payload
        payload = payload["payload"]
        if "business_version" not in payload and envelope.get("business_version"):
            payload = {
                **payload,
                "business_version": envelope["business_version"],
            }

    entry: Mapping[str, Any] = payload
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        candidates = [item for item in accounts if isinstance(item, Mapping)]
        entry = next(
            (item for item in candidates if _entry_account_id(item) == str(account_id)),
            candidates[0] if len(candidates) == 1 else {},
        )
        if not entry:
            raise SimTradingError(f"snapshot does not contain account {account_id}")

    account_raw = entry.get("account")
    if not isinstance(account_raw, Mapping):
        account_raw = entry
    actual_id = _first(account_raw, "account_id", "id") or account_id
    if str(actual_id) != str(account_id):
        raise SimTradingError(
            f"snapshot account mismatch: expected {account_id}, received {actual_id}"
        )

    valuation = entry.get("valuation")
    if not isinstance(valuation, Mapping):
        valuation = {}
    account = AccountSnapshot(
        account_id=str(actual_id),
        account_type=str(_first(account_raw, "account_type", "type") or ""),
        status=str(account_raw.get("status") or ""),
        trading_day=_date(account_raw.get("trading_day")),
        risk_state=_optional_text(
            _first(account_raw, "risk_state") or valuation.get("risk_state")
        ),
        cash_balance=_optional_decimal(account_raw.get("cash_balance")),
        available_cash=_optional_decimal(account_raw.get("available_cash")),
        equity=_optional_decimal(account_raw.get("equity")),
        used_margin=_optional_decimal(account_raw.get("used_margin")),
        frozen_margin=_optional_decimal(account_raw.get("frozen_margin")),
        risk_ratio=_optional_decimal(account_raw.get("risk_ratio")),
    )

    positions_raw = _snapshot_list(entry.get("positions"), "positions")
    normalized_positions = (
        normalize_position(item, account.account_id) for item in positions_raw
    )
    positions = tuple(
        position for position in normalized_positions if position.volume > 0
    )

    orders_raw = _snapshot_list(
        entry.get("active_orders") or entry.get("orders"), "orders"
    )
    active_orders = tuple(
        _active_order(item, account.account_id)
        for item in orders_raw
    )
    version = _first(entry, "business_version", "version") or _first(
        payload, "business_version", "version"
    )
    return PortfolioSnapshot(
        account=account,
        positions=positions,
        active_orders=active_orders,
        business_version=_optional_text(version),
    )


def normalize_confirmed_trade_page(
    raw: Any,
    account_id: str,
    order_strategies: Mapping[str, str],
) -> ConfirmedTradePage:
    payload = _unwrap(raw)
    if not isinstance(payload, Mapping):
        raise SimTradingError("trade page is not an object")
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise SimTradingError("trade page items must be a list of objects")
    fills = tuple(
        normalize_confirmed_trade(item, account_id, order_strategies)
        for item in items
        if str(item.get("order_id") or "") in order_strategies
    )
    has_more = payload.get("has_more")
    if not isinstance(has_more, bool):
        raise SimTradingError("trade page has_more must be boolean")
    next_cursor = payload.get("next_cursor")
    if has_more and not next_cursor:
        raise SimTradingError("trade page has_more without next_cursor")
    return ConfirmedTradePage(
        fills=fills,
        next_cursor=None if next_cursor in (None, "") else str(next_cursor),
        has_more=has_more,
    )


def normalize_order_page(raw: Any, account_id: str) -> BrokerOrderPage:
    payload = _unwrap(raw)
    if not isinstance(payload, Mapping):
        raise SimTradingError("order page is not an object")
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise SimTradingError("order page items must be a list of objects")
    orders = tuple(normalize_order(item, account_id) for item in items)
    has_more = payload.get("has_more")
    if not isinstance(has_more, bool):
        raise SimTradingError("order page has_more must be boolean")
    next_cursor = payload.get("next_cursor")
    if has_more and not next_cursor:
        raise SimTradingError("order page has_more without next_cursor")
    return BrokerOrderPage(
        orders=orders,
        next_cursor=None if next_cursor in (None, "") else str(next_cursor),
        has_more=has_more,
    )


def normalize_order(raw: Mapping[str, Any], account_id: str) -> BrokerOrder:
    raw = _unwrap(raw)
    if not isinstance(raw, Mapping):
        raise SimTradingError("order is not an object")
    actual_account = str(raw.get("account_id") or "")
    if actual_account != str(account_id):
        raise SimTradingError(
            f"order account mismatch: expected {account_id}, received {actual_account}"
        )
    order_id = str(raw.get("order_id") or "")
    client_order_id = str(raw.get("client_order_id") or "")
    instrument = str(_first(raw, "order_book_id", "symbol") or "")
    if not order_id or not client_order_id or not instrument:
        raise SimTradingError(
            "order is missing order_id, client_order_id, or instrument"
        )
    total_volume = _decimal(raw.get("total_volume"), "total_volume")
    if total_volume != total_volume.to_integral_value() or total_volume <= 0:
        raise SimTradingError("total_volume must be a positive integer")
    traded_volume = _whole_volume(raw.get("traded_volume"), "traded_volume")
    remaining_volume = _whole_volume(raw.get("remaining_volume"), "remaining_volume")
    cancelled_volume = _whole_volume(raw.get("cancelled_volume"), "cancelled_volume")
    if traded_volume + remaining_volume + cancelled_volume != int(total_volume):
        raise SimTradingError("broker order volumes do not reconcile")
    raw_price = raw.get("limit_price")
    return BrokerOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        account_id=actual_account,
        exchange_id=str(raw.get("exchange_id") or ""),
        instrument=instrument,
        direction=str(raw.get("direction") or "").upper(),
        offset=str(raw.get("offset_flag") or "").upper(),
        order_type=str(raw.get("order_type") or "").upper(),
        limit_price=(
            None if raw_price in (None, "") else _decimal(raw_price, "limit_price")
        ),
        total_volume=int(total_volume),
        traded_volume=traded_volume,
        remaining_volume=remaining_volume,
        cancelled_volume=cancelled_volume,
        status=str(raw.get("status") or "").upper(),
        created_at=_datetime(raw.get("created_at")),
    )


def normalize_confirmed_trade(
    raw: Mapping[str, Any],
    account_id: str,
    order_strategies: Mapping[str, str],
) -> ConfirmedFill:
    actual_account = str(raw.get("account_id") or "")
    if actual_account != str(account_id):
        raise SimTradingError(
            f"trade account mismatch: expected {account_id}, received {actual_account}"
        )
    trade_id = str(raw.get("trade_id") or "")
    order_id = str(raw.get("order_id") or "")
    instrument = str(raw.get("order_book_id") or "")
    if not trade_id or not order_id or not instrument:
        raise SimTradingError("trade is missing trade_id, order_id, or order_book_id")
    strategy = order_strategies.get(order_id)
    if strategy not in {"ALPHA", "BETA"}:
        raise SimTradingError(f"trade order {order_id} has no Alpha/Beta ownership")
    volume = _decimal(raw.get("trade_volume"), "trade_volume")
    if volume != volume.to_integral_value() or volume <= 0:
        raise SimTradingError("trade_volume must be a positive integer")
    direction = str(raw.get("direction") or "").upper()
    if direction == "BUY":
        quantity = int(volume)
    elif direction == "SELL":
        quantity = -int(volume)
    else:
        raise SimTradingError(f"unknown trade direction: {direction!r}")
    return ConfirmedFill(
        trade_id=trade_id,
        order_id=order_id,
        account_id=actual_account,
        strategy=strategy,
        instrument=instrument,
        quantity=quantity,
        price=_decimal(raw.get("trade_price"), "trade_price"),
        executed_at=_datetime(raw.get("trade_time")),
    )


def normalize_position(
    raw: Mapping[str, Any], account_id: str
) -> PositionSnapshot:
    """Normalize one absolute position from a snapshot or position event."""

    # The trading-snapshot endpoint returns each row as
    # {"position": <absolute position>, "pnl": <realtime valuation>}.
    wrapped = raw.get("position")
    if isinstance(wrapped, Mapping):
        raw = wrapped
    instrument = _first(raw, "order_book_id", "symbol", "code")
    position_id = _first(raw, "position_id", "id")
    if not instrument or not position_id:
        raise SimTradingError(
            "position is missing position_id or instrument; "
            f"received fields: {', '.join(sorted(map(str, raw.keys())))}"
        )
    today_volume = _decimal(raw.get("today_volume", 0), "today_volume")
    yesterday_volume = _decimal(raw.get("yesterday_volume", 0), "yesterday_volume")
    explicit_volume = _first(raw, "volume", "quantity")
    if explicit_volume is None:
        if "today_volume" not in raw and "yesterday_volume" not in raw:
            raise SimTradingError(
                "position has no volume fields; "
                f"received fields: {', '.join(sorted(map(str, raw.keys())))}"
            )
        volume = today_volume + yesterday_volume
    else:
        volume = _decimal(explicit_volume, "volume")
    return PositionSnapshot(
        position_id=str(position_id),
        account_id=str(raw.get("account_id") or account_id),
        instrument=str(instrument),
        direction=str(_first(raw, "direction", "side") or "LONG").upper(),
        volume=volume,
        today_volume=today_volume,
        yesterday_volume=yesterday_volume,
        frozen_volume=_decimal(raw.get("frozen_volume", 0), "frozen_volume"),
        available_volume=_decimal(
            raw.get("available_volume", volume), "available_volume"
        ),
        average_price=_optional_decimal(
            _first(raw, "average_price", "average_open_price", "cost_price")
        ),
    )


def _active_order(raw: Mapping[str, Any], account_id: str) -> ActiveOrderSnapshot:
    instrument = _first(raw, "order_book_id", "symbol", "code")
    order_id = _first(raw, "order_id", "id")
    if not instrument or not order_id:
        raise SimTradingError(
            "active order is missing order_id or instrument; "
            f"received fields: {', '.join(sorted(map(str, raw.keys())))}"
        )
    total = _decimal(_first(raw, "total_volume", "volume", "quantity"), "total_volume")
    traded = _decimal(
        _first(raw, "traded_volume", "filled_quantity") or 0, "traded_volume"
    )
    remaining_value = _first(raw, "remaining_volume", "remaining_quantity")
    remaining = total - traded if remaining_value is None else _decimal(
        remaining_value, "remaining_volume"
    )
    return ActiveOrderSnapshot(
        order_id=str(order_id),
        account_id=str(raw.get("account_id") or account_id),
        instrument=str(instrument),
        status=str(_first(raw, "status", "state") or ""),
        direction=str(_first(raw, "direction", "side") or "").upper(),
        total_volume=total,
        traded_volume=traded,
        remaining_volume=remaining,
        limit_price=_optional_decimal(
            _first(raw, "limit_price", "resolved_price", "price")
        ),
    )


def _unwrap(value: Any) -> Any:
    if isinstance(value, Mapping) and "data" in value:
        success = value.get("success")
        if success is False:
            raise SimTradingError(str(value.get("message") or "simulator request failed"))
        return value["data"]
    return value


def _entry_account_id(entry: Mapping[str, Any]) -> str | None:
    account = entry.get("account")
    if isinstance(account, Mapping):
        value = _first(account, "account_id", "id")
    else:
        value = _first(entry, "account_id", "id")
    return None if value is None else str(value)


def _snapshot_list(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    """Validate the arrays used by REST and WebSocket trading snapshots."""

    if value is None:
        return ()
    if not isinstance(value, list):
        raise SimTradingError(f"{name} in a trading snapshot is not a list")
    if not all(isinstance(record, Mapping) for record in value):
        raise SimTradingError(f"{name} collection contains a non-object record")
    return tuple(value)


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None:
            return candidate
    return None


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise SimTradingError(f"{field} is missing or invalid")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SimTradingError(f"{field} is not decimal: {value!r}") from exc


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else _decimal(value, "decimal field")


def _whole_volume(value: Any, field: str) -> int:
    number = _decimal(value, field)
    if number != number.to_integral_value() or number < 0:
        raise SimTradingError(f"{field} must be a non-negative integer")
    return int(number)


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise SimTradingError(f"invalid trading_day: {value!r}") from exc


def _datetime(value: Any) -> datetime:
    if value is None or value == "":
        raise SimTradingError("trade_time is missing")
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SimTradingError(f"invalid trade_time: {value!r}") from exc
    if result.tzinfo is None:
        raise SimTradingError("trade_time must be timezone-aware")
    return result


def _optional_text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)
