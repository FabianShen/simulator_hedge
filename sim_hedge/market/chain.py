"""Application logic for inspecting normalized option chains."""

from dataclasses import dataclass
from datetime import date
from itertools import groupby

from sim_hedge.domain.contract import OptionContract, OptionType


@dataclass(frozen=True)
class ExpirySummary:
    maturity: date
    calls: int
    puts: int
    minimum_strike: float
    maximum_strike: float


def summarize(contracts: list[OptionContract]) -> list[ExpirySummary]:
    summaries = []
    ordered = sorted(contracts, key=lambda contract: (contract.maturity, contract.strike))
    for maturity, group in groupby(ordered, key=lambda contract: contract.maturity):
        items = list(group)
        strikes = [contract.strike for contract in items]
        summaries.append(
            ExpirySummary(
                maturity=maturity,
                calls=sum(contract.option_type is OptionType.CALL for contract in items),
                puts=sum(contract.option_type is OptionType.PUT for contract in items),
                minimum_strike=min(strikes),
                maximum_strike=max(strikes),
            )
        )
    return summaries


def subscription(
    underlying: str,
    contracts: list[OptionContract],
) -> list[str]:
    """Subscript underlying to the option chain with single identifier"""

    if any(contract.underlying != underlying for contract in contracts):
        raise ValueError("all option contracts must belong to the requested underlying")
    return list(dict.fromkeys([underlying, *(contract.instrument for contract in contracts)]))
