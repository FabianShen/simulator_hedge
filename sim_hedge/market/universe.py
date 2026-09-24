"""Select the option contracts required by a pricing strategy."""

from dataclasses import dataclass
from datetime import date

from sim_hedge.domain.contract import OptionContract, OptionType


@dataclass(frozen=True)
class StrategyUniverse:
    """A small, explicit subset of one underlying's option chain."""

    underlying: str
    maturity: date
    center_strike: float
    strikes: tuple[float, ...]
    contracts: tuple[OptionContract, ...]

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(contract.instrument for contract in self.contracts)


def select_strategy_universe(
    contracts: list[OptionContract],
    spot: float,
    *,
    expiry_index: int = 0,
    strike_wings: int = 2,
) -> StrategyUniverse:
    """Select complete call/put pairs nearest DTE."""

    if not contracts:
        raise ValueError("option chain must not be empty")
    if spot <= 0:
        raise ValueError("spot must be positive")
    if expiry_index < 0:
        raise ValueError("expiry_index must not be negative")
    if strike_wings < 0:
        raise ValueError("strike_wings must not be negative")

    underlyings = {contract.underlying for contract in contracts}
    if len(underlyings) != 1:
        raise ValueError("all contracts must have the same underlying")

    maturities = sorted({contract.maturity for contract in contracts})
    if expiry_index >= len(maturities):
        raise ValueError(
            f"expiry_index {expiry_index} is outside the {len(maturities)} maturities"
        )
    maturity = maturities[expiry_index]

    pairs: dict[float, dict[OptionType, OptionContract]] = {}
    for contract in contracts:
        if contract.maturity != maturity:
            continue
        by_type = pairs.setdefault(contract.strike, {})
        if contract.option_type in by_type:
            raise ValueError(
                f"duplicate {contract.option_type.value} at strike {contract.strike:g}"
            )
        by_type[contract.option_type] = contract

    complete_strikes = sorted(
        strike
        for strike, pair in pairs.items()
        if OptionType.CALL in pair and OptionType.PUT in pair
    )
    if not complete_strikes:
        raise ValueError(f"no complete call/put pairs for maturity {maturity}")

    center_index = min(
        range(len(complete_strikes)),
        key=lambda index: (abs(complete_strikes[index] - spot), complete_strikes[index]),
    )
    first = max(0, center_index - strike_wings)
    last = min(len(complete_strikes), center_index + strike_wings + 1)
    selected_strikes = tuple(complete_strikes[first:last])
    selected_contracts = tuple(
        pairs[strike][option_type]
        for strike in selected_strikes
        for option_type in (OptionType.CALL, OptionType.PUT)
    )

    return StrategyUniverse(
        underlying=next(iter(underlyings)),
        maturity=maturity,
        center_strike=complete_strikes[center_index],
        strikes=selected_strikes,
        contracts=selected_contracts,
    )
