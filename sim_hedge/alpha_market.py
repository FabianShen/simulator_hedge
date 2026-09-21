"""Build the available-chain market input used for Alpha initialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sim_hedge.domain import OptionContract
from sim_hedge.market_state import MarketState
from sim_hedge.pricing_request import PricingRequestError


def build_alpha_market_snapshot(
    *,
    snapshot_id: str,
    as_of: datetime,
    underlying: str,
    contracts: list[OptionContract],
    market_state: MarketState,
    max_quote_age: timedelta = timedelta(seconds=5),
    feed_unsafe: bool = False,
) -> dict[str, Any]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if feed_unsafe:
        raise PricingRequestError("market feed is unsafe")
    maturity = min(contract.maturity for contract in contracts)
    nearest = [contract for contract in contracts if contract.maturity == maturity]
    quotes = market_state.snapshot()
    if underlying not in quotes:
        raise PricingRequestError("Alpha market is missing the underlying quote")
    underlying_quote = quotes[underlying]
    if as_of - underlying_quote.received_at > max_quote_age:
        raise PricingRequestError("underlying quote is stale")
    if underlying_quote.previous_close is None:
        raise PricingRequestError("underlying prev_close is missing")
    if underlying_quote.trading_date is None:
        raise PricingRequestError("underlying trading_date is missing")
    spot = _price(underlying_quote, two_sided=False)
    options = []
    for contract in nearest:
        quote = quotes.get(contract.instrument)
        if (
            quote is None
            or quote.previous_settlement is None
            or quote.trading_date != underlying_quote.trading_date
        ):
            continue
        try:
            market_price = _price(quote, two_sided=False)
        except PricingRequestError:
            market_price = quote.previous_settlement
        options.append(
            {
                "instrument": contract.instrument,
                "optionType": f"OPTION_TYPE_{contract.option_type.value}",
                "strike": contract.strike,
                "expiry": datetime.combine(
                    contract.maturity,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
                "contractMultiplier": contract.contract_multiplier,
                "priceTick": contract.price_tick,
                "marketPrice": market_price,
                "previousSettlement": quote.previous_settlement,
            }
        )
    if not options:
        raise PricingRequestError("no fresh nearest-expiry option margin inputs")
    return {
        "requestId": snapshot_id,
        "asOf": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tradingDate": underlying_quote.trading_date.isoformat(),
        "underlying": {
            "instrument": underlying,
            "spot": spot,
            "previousClose": underlying_quote.previous_close,
        },
        "options": options,
    }


def _price(quote, *, two_sided: bool) -> float:
    if quote.bid is not None and quote.ask is not None:
        if quote.bid > quote.ask:
            raise PricingRequestError(f"crossed market for {quote.instrument}")
        return (quote.bid + quote.ask) / 2
    if not two_sided and quote.last is not None:
        return quote.last
    raise PricingRequestError(f"no usable price for {quote.instrument}")
