"""Pure construction of an initial equal-contract short-OTM Alpha target."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sim_hedge.domain.contract import OptionContract, OptionType


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
    margin_budget: Decimal
    one_contract_basket_margin: Decimal
    margin_capacity: int
    max_contracts_per_option: int | None
    contracts_per_option: int
    legs: tuple[AlphaLeg, ...]

    @property
    def target_positions(self) -> dict[str, int]:
        return {leg.contract.instrument: leg.quantity for leg in self.legs}


def build_short_otm_alpha_plan(
    contracts: list[OptionContract],
    *,
    spot: float,
    previous_underlying_close: Decimal | float | int | str,
    previous_settlements: Mapping[str, Decimal | float | int | str],
    initial_cash: Decimal | float | int | str,
    budget_fraction: Decimal | float | str = Decimal("0.30"),
    max_contracts_per_option: int | None = None,
) -> AlphaPlan:
    """Build a balanced nearest-expiry basket sized by short-option margin."""

    if not contracts:
        raise AlphaPlanError("option chain must not be empty")
    if spot <= 0:
        raise AlphaPlanError("spot must be positive")
    cash = _decimal(initial_cash, "initial_cash")
    fraction = _decimal(budget_fraction, "budget_fraction")
    previous_close = _decimal(previous_underlying_close, "previous_underlying_close")
    if cash <= 0:
        raise AlphaPlanError("initial_cash must be positive")
    if previous_close <= 0:
        raise AlphaPlanError("previous_underlying_close must be positive")
    if not Decimal("0") < fraction <= Decimal("1"):
        raise AlphaPlanError("budget_fraction must be greater than zero and at most one")
    if max_contracts_per_option is not None and max_contracts_per_option < 1:
        raise AlphaPlanError("max_contracts_per_option must be positive")

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

    basket_margin = sum(
        (
            short_option_opening_margin(
                contract,
                previous_underlying_close=previous_close,
                previous_settlement=_price(
                    previous_settlements, contract.instrument
                ),
            )
            for contract in selected
        ),
        start=Decimal("0"),
    )
    budget = cash * fraction
    margin_capacity = int(budget // basket_margin)
    if margin_capacity < 1:
        raise AlphaPlanError(
            "margin budget cannot fund one short contract per selected option"
        )
    contracts_per_option = (
        margin_capacity
        if max_contracts_per_option is None
        else min(margin_capacity, max_contracts_per_option)
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
        margin_budget=budget,
        one_contract_basket_margin=basket_margin,
        margin_capacity=margin_capacity,
        max_contracts_per_option=max_contracts_per_option,
        contracts_per_option=contracts_per_option,
        legs=legs,
    )


def short_option_opening_margin(
    contract: OptionContract,
    *,
    previous_underlying_close: Decimal | float | int | str,
    previous_settlement: Decimal | float | int | str,
) -> Decimal:
    """Return opening margin for one short option contract."""

    underlying_close = _decimal(
        previous_underlying_close, "previous_underlying_close"
    )
    settlement = _decimal(previous_settlement, "previous_settlement")
    if underlying_close <= 0 or settlement <= 0:
        raise AlphaPlanError("previous close and settlement must be positive")
    strike = Decimal(str(contract.strike))
    multiplier = Decimal(contract.contract_multiplier)
    if contract.option_type is OptionType.CALL:
        out_of_money = max(strike - underlying_close, Decimal("0"))
        per_unit = settlement + max(
            Decimal("0.12") * underlying_close - out_of_money,
            Decimal("0.07") * underlying_close,
        )
    else:
        out_of_money = max(underlying_close - strike, Decimal("0"))
        per_unit = min(
            settlement
            + max(
                Decimal("0.12") * underlying_close - out_of_money,
                Decimal("0.07") * strike,
            ),
            strike,
        )
    return per_unit * multiplier


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
