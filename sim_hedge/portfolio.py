"""Normalized portfolio values independent of the simulated-trading API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    account_type: str
    status: str
    trading_day: date | None = None
    risk_state: str | None = None
    cash_balance: Decimal | None = None
    available_cash: Decimal | None = None
    equity: Decimal | None = None
    used_margin: Decimal | None = None
    frozen_margin: Decimal | None = None
    risk_ratio: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must not be empty")


@dataclass(frozen=True)
class PositionSnapshot:
    position_id: str
    account_id: str
    instrument: str
    direction: str
    volume: Decimal
    today_volume: Decimal
    yesterday_volume: Decimal
    frozen_volume: Decimal
    available_volume: Decimal
    average_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.position_id or not self.account_id or not self.instrument:
            raise ValueError("position_id, account_id, and instrument must not be empty")
        if min(
            self.volume,
            self.today_volume,
            self.yesterday_volume,
            self.frozen_volume,
            self.available_volume,
        ) < 0:
            raise ValueError("position quantities must not be negative")

    @property
    def signed_volume(self) -> Decimal:
        if self.direction.upper() in {"SHORT", "SELL"}:
            return -self.volume
        return self.volume


@dataclass(frozen=True)
class ActiveOrderSnapshot:
    order_id: str
    account_id: str
    instrument: str
    status: str
    direction: str
    total_volume: Decimal
    traded_volume: Decimal
    remaining_volume: Decimal
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.account_id or not self.instrument:
            raise ValueError("order_id, account_id, and instrument must not be empty")
        if min(self.total_volume, self.traded_volume, self.remaining_volume) < 0:
            raise ValueError("order quantities must not be negative")


@dataclass(frozen=True)
class PortfolioSnapshot:
    """One authoritative absolute snapshot from the trading system."""

    account: AccountSnapshot
    positions: tuple[PositionSnapshot, ...]
    active_orders: tuple[ActiveOrderSnapshot, ...]
    business_version: str | None = None

    @property
    def instruments(self) -> tuple[str, ...]:
        """Instruments that market/pricing must retain, including open orders."""

        return tuple(
            sorted(
                {
                    *(position.instrument for position in self.positions),
                    *(order.instrument for order in self.active_orders),
                }
            )
        )

    @property
    def signed_positions(self) -> dict[str, int]:
        result: dict[str, Decimal] = {}
        for position in self.positions:
            result[position.instrument] = (
                result.get(position.instrument, Decimal(0))
                + position.signed_volume
            )
        normalized: dict[str, int] = {}
        for instrument, quantity in result.items():
            if quantity != quantity.to_integral_value():
                raise ValueError(f"broker position {instrument} is not an integer")
            if quantity:
                normalized[instrument] = int(quantity)
        return dict(sorted(normalized.items()))
