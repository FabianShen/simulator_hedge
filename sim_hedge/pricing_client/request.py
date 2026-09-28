"""Build vendor-neutral pricing requests from a ready market snapshot."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hedge_engine import StrategyLedger
from sim_hedge.domain.contract import OptionContract
from sim_hedge.domain.quote import MarketQuote
from sim_hedge.market.state import MarketState
from sim_hedge.market.universe import StrategyUniverse
from sim_hedge.jsonio import write_json


class PricingRequestError(RuntimeError):
    """A safe and complete pricing request cannot be built."""


def held_valuation_contracts(
    ledger: StrategyLedger, contracts: Iterable[OptionContract]
) -> tuple[OptionContract, ...]:
    """Resolve every confirmed holding against the reference contract chain."""

    by_code = {contract.instrument: contract for contract in contracts}
    held = set(ledger.alpha_positions) | set(ledger.beta_positions)
    missing = held - set(by_code)
    if missing:
        raise PricingRequestError(
            "held positions are absent from the option chain: "
            + ", ".join(sorted(missing))
        )
    return tuple(by_code[code] for code in sorted(held))


@dataclass(frozen=True)
class PricingRequestPolicy:
    risk_free_rate: float
    dividend_yield: float
    beta: float = 0.5
    minimum_strikes: int = 3
    max_quote_age: timedelta = timedelta(seconds=5)
    expiry_time: time = time(15, 0)
    market_timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if not 0 <= self.beta <= 1:
            raise ValueError("beta must be between zero and one")
        if self.minimum_strikes < 3:
            raise ValueError("minimum_strikes must be at least three")
        if self.max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be positive")
        try:
            ZoneInfo(self.market_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"unknown market timezone: {self.market_timezone}"
            ) from exc


def build_pricing_request(
    *,
    request_id: str,
    as_of: datetime,
    market_state: MarketState,
    universe: StrategyUniverse,
    policy: PricingRequestPolicy,
    feed_unsafe: bool = False,
    hedge_contracts: Iterable[OptionContract] = (),
    valuation_contracts: Iterable[OptionContract] = (),
) -> dict[str, Any]:
    """Price quoted near-expiry candidates and value unquoted held options."""

    if not request_id:
        raise ValueError("request_id must not be empty")
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if feed_unsafe:
        raise PricingRequestError("market feed is unsafe")
    required = (universe.underlying, *universe.instruments)
    activated = set(market_state.required_instruments)
    if not set(required).issubset(activated):
        raise PricingRequestError("strategy universe has not been activated")

    snapshots = market_state.snapshot()
    missing = [instrument for instrument in required if instrument not in snapshots]
    if missing:
        raise PricingRequestError(f"missing quotes: {', '.join(missing)}")
    stale = [
        instrument
        for instrument in required
        if as_of - snapshots[instrument].received_at > policy.max_quote_age
    ]
    if stale:
        raise PricingRequestError(f"stale quotes: {', '.join(stale)}")

    if len(universe.strikes) < policy.minimum_strikes:
        raise PricingRequestError(
            f"universe has {len(universe.strikes)} strikes; "
            f"{policy.minimum_strikes} required"
        )

    market_zone = ZoneInfo(policy.market_timezone)
    expiry = datetime.combine(
        universe.maturity,
        policy.expiry_time,
        tzinfo=market_zone,
    ).astimezone(timezone.utc)
    as_of_utc = as_of.astimezone(timezone.utc)
    if expiry <= as_of_utc:
        raise PricingRequestError("option expiry must be after as_of")

    underlying_quote = snapshots[universe.underlying]
    spot = _reference_price(underlying_quote, require_two_sided=False)[0]
    options = []
    for contract in universe.contracts:
        quote = snapshots[contract.instrument]
        market_price, price_source = _reference_price(
            quote,
            require_two_sided=True,
        )
        options.append(
            {
                "instrument": contract.instrument,
                "optionType": f"OPTION_TYPE_{contract.option_type.value}",
                "exerciseStyle": "EXERCISE_STYLE_EUROPEAN",
                "strike": contract.strike,
                "expiry": _utc_text(expiry),
                "observedAt": _observed_at_utc(quote, market_zone),
                "contractMultiplier": contract.contract_multiplier,
                "priceTick": contract.price_tick,
                "marketPrice": market_price,
                "marketPriceSource": price_source,
            }
        )

    included = set(universe.instruments)
    # Same-expiry contracts with fresh two-sided quotes are additional hedge
    # candidates. Missing or illiquid contracts are simply not eligible.
    for contract in hedge_contracts:
        if contract.instrument in included:
            continue
        if contract.underlying != universe.underlying or contract.maturity != universe.maturity:
            continue
        quote = snapshots.get(contract.instrument)
        if quote is None or as_of - quote.received_at > policy.max_quote_age:
            continue
        try:
            market_price, price_source = _reference_price(
                quote, require_two_sided=True
            )
        except PricingRequestError:
            continue
        included.add(contract.instrument)
        options.append(
            {
                "instrument": contract.instrument,
                "optionType": f"OPTION_TYPE_{contract.option_type.value}",
                "exerciseStyle": "EXERCISE_STYLE_EUROPEAN",
                "strike": contract.strike,
                "expiry": _utc_text(expiry),
                "observedAt": _observed_at_utc(quote, market_zone),
                "contractMultiplier": contract.contract_multiplier,
                "priceTick": contract.price_tick,
                "marketPrice": market_price,
                "marketPriceSource": price_source,
            }
        )

    for contract in valuation_contracts:
        if contract.instrument in included:
            continue
        if contract.underlying != universe.underlying or contract.maturity != universe.maturity:
            raise PricingRequestError(
                f"held option {contract.instrument} is outside the pricing expiry or underlying"
            )
        included.add(contract.instrument)
        # No marketPrice: this option receives model Greeks but cannot distort
        # SABR calibration with a missing, stale, or illiquid quote.
        options.append(
            {
                "instrument": contract.instrument,
                "optionType": f"OPTION_TYPE_{contract.option_type.value}",
                "exerciseStyle": "EXERCISE_STYLE_EUROPEAN",
                "strike": contract.strike,
                "expiry": _utc_text(expiry),
                "contractMultiplier": contract.contract_multiplier,
                "priceTick": contract.price_tick,
            }
        )

    return {
        "requestId": request_id,
        "asOf": _utc_text(as_of_utc),
        "underlying": {
            "instrument": universe.underlying,
            "spot": spot,
            "observedAt": _observed_at_utc(underlying_quote, market_zone),
        },
        "options": options,
        "assumptions": {
            "riskFreeRate": policy.risk_free_rate,
            "dividendYield": policy.dividend_yield,
            "dayCount": "DAY_COUNT_ACT_365_FIXED",
        },
        "configuration": {
            "model": "PRICING_MODEL_SABR_BLACK_76",
            "calculateImpliedVolatility": True,
            "sabr": {
                "beta": policy.beta,
                "minimumStrikes": policy.minimum_strikes,
            },
        },
    }


def record_pricing_request(path: str | Path, request: dict[str, Any]) -> None:
    """Atomically record a request that can be replayed by the pricing engine."""

    write_json(path, request, ensure_ascii=False, trailing_newline=False)


def _reference_price(
    quote: MarketQuote,
    *,
    require_two_sided: bool,
) -> tuple[float, str]:
    if quote.bid is not None and quote.ask is not None:
        if quote.bid > quote.ask:
            raise PricingRequestError(f"crossed market for {quote.instrument}")
        return (quote.bid + quote.ask) / 2.0, "MARKET_PRICE_SOURCE_MID"
    if require_two_sided:
        raise PricingRequestError(f"two-sided market required for {quote.instrument}")
    if quote.last is not None:
        return quote.last, "MARKET_PRICE_SOURCE_LAST"
    raise PricingRequestError(f"no usable reference price for {quote.instrument}")


def _observed_at_utc(quote: MarketQuote, market_zone: ZoneInfo) -> str:
    observed_at = quote.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=market_zone)
    return _utc_text(observed_at.astimezone(timezone.utc))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
