"""Pure construction of an initial equal-contract short-OTM Alpha target."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sim_hedge.domain import OptionContract, OptionType


class AlphaPlanError(ValueError):
    pass


@dataclass(frozen=True)
class AlphaLeg:
    contract: OptionContract
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity >= 0:
            raise ValueError("an initial short Alpha quantity must be negative")


@dataclass(frozen=True)
class AlphaPlan:
    underlying: str
    maturity: date
    initial_cash: Decimal
    budget_fraction: Decimal
    premium_equivalent_budget: Decimal
    one_contract_basket_value: Decimal
    contracts_per_option: int
    legs: tuple[AlphaLeg, ...]

    @property
    def target_positions(self) -> dict[str, int]:
        return {leg.contract.instrument: leg.quantity for leg in self.legs}


def build_short_otm_alpha_plan(
    contracts: list[OptionContract],
    *,
    spot: float,
    reference_prices: Mapping[str, Decimal | float | int | str],
    initial_cash: Decimal | float | int | str,
    budget_fraction: Decimal | float | str = Decimal("0.30"),
) -> AlphaPlan:
    """Build a balanced nearest-expiry basket with one quantity for every leg.

    The budget is a premium-equivalent sizing rule. It is not an estimate of
    the margin required by the simulated-trading system.
    """

    if not contracts:
        raise AlphaPlanError("option chain must not be empty")
    if spot <= 0:
        raise AlphaPlanError("spot must be positive")
    cash = _decimal(initial_cash, "initial_cash")
    fraction = _decimal(budget_fraction, "budget_fraction")
    if cash <= 0:
        raise AlphaPlanError("initial_cash must be positive")
    if not Decimal("0") < fraction <= Decimal("1"):
        raise AlphaPlanError("budget_fraction must be greater than zero and at most one")

    underlyings = {contract.underlying for contract in contracts}
    if len(underlyings) != 1:
        raise AlphaPlanError("all contracts must have the same underlying")
    maturity = min(contract.maturity for contract in contracts)
    nearest = [contract for contract in contracts if contract.maturity == maturity]
    calls = sorted(
        (
            contract
            for contract in nearest
            if contract.option_type is OptionType.CALL and contract.strike > spot
        ),
        key=lambda contract: contract.strike,
    )
    puts = sorted(
        (
            contract
            for contract in nearest
            if contract.option_type is OptionType.PUT and contract.strike < spot
        ),
        key=lambda contract: contract.strike,
        reverse=True,
    )
    level_count = min(len(calls), len(puts))
    if level_count == 0:
        raise AlphaPlanError("nearest maturity has no balanced OTM call/put set")
    selected = tuple(calls[:level_count] + puts[:level_count])

    basket_value = sum(
        (
            _price(reference_prices, contract.instrument)
            * contract.contract_multiplier
            for contract in selected
        ),
        start=Decimal("0"),
    )
    budget = cash * fraction
    contracts_per_option = int(budget // basket_value)
    if contracts_per_option < 1:
        raise AlphaPlanError(
            "30% budget cannot fund one premium-equivalent contract per option"
        )
    legs = tuple(
        AlphaLeg(contract=contract, quantity=-contracts_per_option)
        for contract in selected
    )
    return AlphaPlan(
        underlying=next(iter(underlyings)),
        maturity=maturity,
        initial_cash=cash,
        budget_fraction=fraction,
        premium_equivalent_budget=budget,
        one_contract_basket_value=basket_value,
        contracts_per_option=contracts_per_option,
        legs=legs,
    )


def _price(
    prices: Mapping[str, Decimal | float | int | str], instrument: str
) -> Decimal:
    if instrument not in prices:
        raise AlphaPlanError(f"missing reference price for {instrument}")
    price = _decimal(prices[instrument], f"price for {instrument}")
    if price <= 0:
        raise AlphaPlanError(f"price for {instrument} must be positive")
    return price


def _decimal(value: Decimal | float | int | str, field: str) -> Decimal:
    if isinstance(value, bool):
        raise AlphaPlanError(f"{field} is not decimal")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AlphaPlanError(f"{field} is not decimal") from exc
