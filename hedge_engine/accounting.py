"""Pure Alpha/Beta position accounting from confirmed broker fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence


STRATEGIES = {"ALPHA", "BETA"}


@dataclass(frozen=True)
class ConfirmedFill:
    trade_id: str
    order_id: str
    account_id: str
    strategy: str
    instrument: str
    quantity: int
    price: Decimal
    executed_at: datetime

    def __post_init__(self) -> None:
        if not self.trade_id or not self.order_id or not self.account_id:
            raise ValueError("trade_id, order_id, and account_id must not be empty")
        if self.strategy not in STRATEGIES:
            raise ValueError("fill strategy must be ALPHA or BETA")
        if not self.instrument:
            raise ValueError("fill instrument must not be empty")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ValueError("fill quantity must be a signed integer")
        if self.quantity == 0:
            raise ValueError("fill quantity must not be zero")
        if self.price <= 0:
            raise ValueError("fill price must be positive")
        if self.executed_at.tzinfo is None:
            raise ValueError("fill executed_at must be timezone-aware")


@dataclass(frozen=True)
class StrategyLedger:
    account_id: str
    revision: int
    alpha_positions: Mapping[str, int]
    beta_positions: Mapping[str, int]
    applied_trades: Mapping[str, ConfirmedFill]

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("ledger account_id must not be empty")
        if self.revision < 0:
            raise ValueError("ledger revision must not be negative")
        overlap = set(self.alpha_positions) & set(self.beta_positions)
        if overlap:
            raise ValueError("Alpha and Beta ledger instruments must be disjoint")
        for trade_id, fill in self.applied_trades.items():
            if trade_id != fill.trade_id or fill.account_id != self.account_id:
                raise ValueError("applied trade does not belong to this ledger")
        expected_alpha: dict[str, int] = {}
        expected_beta: dict[str, int] = {}
        for fill in self.applied_trades.values():
            book = expected_alpha if fill.strategy == "ALPHA" else expected_beta
            book[fill.instrument] = book.get(fill.instrument, 0) + fill.quantity
        expected_alpha = {code: value for code, value in expected_alpha.items() if value}
        expected_beta = {code: value for code, value in expected_beta.items() if value}
        if dict(self.alpha_positions) != expected_alpha:
            raise ValueError("Alpha positions do not reconcile to applied trades")
        if dict(self.beta_positions) != expected_beta:
            raise ValueError("Beta positions do not reconcile to applied trades")


def empty_ledger(account_id: str) -> StrategyLedger:
    return StrategyLedger(account_id, 0, {}, {}, {})


def combined_strategy_positions(ledger: StrategyLedger) -> dict[str, int]:
    """Combine the disjoint Alpha and Beta books into broker-facing positions."""

    result = dict(ledger.alpha_positions)
    for instrument, quantity in ledger.beta_positions.items():
        result[instrument] = result.get(instrument, 0) + quantity
    return dict(sorted((key, value) for key, value in result.items() if value))


def apply_confirmed_fills(
    ledger: StrategyLedger, fills: Sequence[ConfirmedFill]
) -> StrategyLedger:
    """Apply each trade once and preserve disjoint Alpha/Beta ownership."""

    alpha = dict(ledger.alpha_positions)
    beta = dict(ledger.beta_positions)
    applied = dict(ledger.applied_trades)
    changed = False
    for fill in fills:
        if fill.account_id != ledger.account_id:
            raise ValueError("fill and ledger account IDs do not match")
        previous = applied.get(fill.trade_id)
        if previous is not None:
            if previous != fill:
                raise ValueError(f"trade_id {fill.trade_id} has conflicting contents")
            continue
        own, other = (alpha, beta) if fill.strategy == "ALPHA" else (beta, alpha)
        if fill.instrument in other:
            raise ValueError(
                f"{fill.instrument} is already owned by the other strategy book"
            )
        quantity = own.get(fill.instrument, 0) + fill.quantity
        if quantity:
            own[fill.instrument] = quantity
        else:
            own.pop(fill.instrument, None)
        applied[fill.trade_id] = fill
        changed = True
    if not changed:
        return ledger
    return StrategyLedger(
        account_id=ledger.account_id,
        revision=ledger.revision + 1,
        alpha_positions=alpha,
        beta_positions=beta,
        applied_trades=applied,
    )
