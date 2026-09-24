import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from pricing_engine import SabrParameters, SabrPricingEngine
from pricing_engine.__main__ import load_request
from pricing_engine.black76 import black76_price
from pricing_engine.sabr import sabr_volatility
from sim_hedge.domain import MarketQuote, OptionContract, OptionType
from sim_hedge.market_state import MarketState
from sim_hedge.pricing_request import (
    PricingRequestError,
    PricingRequestPolicy,
    build_pricing_request,
    held_valuation_contracts,
    record_pricing_request,
)
from hedge_engine import ConfirmedFill, apply_confirmed_fills, empty_ledger
from sim_hedge.strategy_universe import StrategyUniverse


NOW = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)
MATURITY = date(2026, 10, 17)


class PricingRequestBuilderTests(unittest.TestCase):
    def test_held_contract_lookup_fails_closed_when_chain_is_incomplete(self) -> None:
        state, universe = _ready_market()
        del state
        ledger = apply_confirmed_fills(empty_ledger("A1"), (
            ConfirmedFill(
                "T1", "O1", "A1", "ALPHA", "MISSING", -1,
                Decimal("0.1"), NOW,
            ),
        ))
        with self.assertRaisesRegex(PricingRequestError, "absent from the option chain"):
            held_valuation_contracts(ledger, universe.contracts)

    def test_held_option_from_another_expiry_is_rejected(self) -> None:
        state, universe = _ready_market()
        other = OptionContract(
            "OTHER-DTE", "UNDERLYING", OptionType.PUT, 3.0,
            date(2026, 11, 17), 10_000, 0.0001,
        )
        with self.assertRaisesRegex(PricingRequestError, "outside the pricing expiry"):
            build_pricing_request(
                request_id="other-dte", as_of=NOW, market_state=state,
                universe=universe, policy=_policy(), valuation_contracts=(other,),
            )

    def test_held_option_without_quote_is_valued_but_not_calibrated(self) -> None:
        state, universe = _ready_market()
        held = OptionContract(
            "HELD-FAR", "UNDERLYING", OptionType.CALL, 3.9, MATURITY,
            10_000, 0.0001,
        )
        request = build_pricing_request(
            request_id="with-held", as_of=NOW, market_state=state,
            universe=universe, policy=_policy(), valuation_contracts=(held,),
        )
        extra = request["options"][-1]
        self.assertEqual(extra["instrument"], "HELD-FAR")
        self.assertNotIn("marketPrice", extra)
        self.assertNotIn("observedAt", extra)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            record_pricing_request(path, request)
            response = SabrPricingEngine().price(load_request(path))
        self.assertEqual(response.calibration.valid_strikes, 3)
        result = next(item for item in response.results if item.instrument == "HELD-FAR")
        self.assertEqual(result.status, "OK")
        self.assertIsNone(result.market_implied_volatility)
        self.assertIsNotNone(result.delta)
        self.assertIsNotNone(result.gamma)

    def test_adds_fresh_quoted_near_expiry_contract_as_hedge_candidate(self) -> None:
        state, universe = _ready_market()
        wing = OptionContract(
            "WING", "UNDERLYING", OptionType.CALL, 3.9, MATURITY,
            10_000, 0.0001,
        )
        state.apply_quote(_quote("WING", last=0.01, bid=0.009, ask=0.011))

        request = build_pricing_request(
            request_id="full-chain", as_of=NOW, market_state=state,
            universe=universe, policy=_policy(), hedge_contracts=(wing,),
        )

        added = next(item for item in request["options"] if item["instrument"] == "WING")
        self.assertAlmostEqual(added["marketPrice"], 0.01)
        self.assertIn("observedAt", added)

    def test_skips_unquoted_or_stale_extra_hedge_contract(self) -> None:
        state, universe = _ready_market()
        unquoted = OptionContract(
            "NO-QUOTE", "UNDERLYING", OptionType.PUT, 2.9, MATURITY,
            10_000, 0.0001,
        )

        request = build_pricing_request(
            request_id="skip-extra", as_of=NOW, market_state=state,
            universe=universe, policy=_policy(), hedge_contracts=(unquoted,),
        )

        self.assertNotIn("NO-QUOTE", {item["instrument"] for item in request["options"]})

    def test_builds_protocol_request_from_ready_market(self) -> None:
        state, universe = _ready_market()

        request = build_pricing_request(
            request_id="offline-001",
            as_of=NOW,
            market_state=state,
            universe=universe,
            policy=_policy(),
        )

        self.assertEqual(request["asOf"], "2026-09-17T02:30:00Z")
        self.assertEqual(request["underlying"]["spot"], 3.3)
        self.assertEqual(request["underlying"]["observedAt"], request["asOf"])
        self.assertEqual(len(request["options"]), 6)
        self.assertEqual(
            {option["expiry"] for option in request["options"]},
            {"2026-10-17T07:00:00Z"},
        )
        self.assertTrue(
            all(
                option["marketPriceSource"] == "MARKET_PRICE_SOURCE_MID"
                for option in request["options"]
            )
        )

    def test_refuses_unsafe_or_stale_market(self) -> None:
        state, universe = _ready_market()

        with self.assertRaisesRegex(PricingRequestError, "unsafe"):
            build_pricing_request(
                request_id="unsafe",
                as_of=NOW,
                market_state=state,
                universe=universe,
                policy=_policy(),
                feed_unsafe=True,
            )

        with self.assertRaisesRegex(PricingRequestError, "stale"):
            build_pricing_request(
                request_id="stale",
                as_of=NOW + timedelta(seconds=6),
                market_state=state,
                universe=universe,
                policy=_policy(),
            )

    def test_refuses_unactivated_universe_and_crossed_market(self) -> None:
        state, universe = _ready_market()
        state.set_required(["UNDERLYING"])

        with self.assertRaisesRegex(PricingRequestError, "not been activated"):
            build_pricing_request(
                request_id="inactive",
                as_of=NOW,
                market_state=state,
                universe=universe,
                policy=_policy(),
            )

        state.set_required(["UNDERLYING", *universe.instruments])
        instrument = universe.instruments[0]
        state.apply_quote(_quote(instrument, last=0.1, bid=0.11, ask=0.10))
        with self.assertRaisesRegex(PricingRequestError, "crossed market"):
            build_pricing_request(
                request_id="crossed",
                as_of=NOW,
                market_state=state,
                universe=universe,
                policy=_policy(),
            )

    def test_recorded_request_replays_in_engine(self) -> None:
        state, universe = _ready_market()
        request = build_pricing_request(
            request_id="replay-001",
            as_of=NOW,
            market_state=state,
            universe=universe,
            policy=_policy(),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            record_pricing_request(path, request)
            recorded = json.loads(path.read_text(encoding="utf-8"))
            response = SabrPricingEngine().price(load_request(path))

        self.assertEqual(recorded, request)
        self.assertEqual(response.calibration.valid_strikes, 3)
        self.assertEqual(len(response.results), 6)
        self.assertTrue(all(result.status == "OK" for result in response.results))


def _policy() -> PricingRequestPolicy:
    return PricingRequestPolicy(
        risk_free_rate=0.015,
        dividend_yield=0.0,
        beta=0.5,
        minimum_strikes=3,
    )


def _ready_market() -> tuple[MarketState, StrategyUniverse]:
    contracts = tuple(
        OptionContract(
            instrument=f"{option_type.value}-{strike}",
            underlying="UNDERLYING",
            option_type=option_type,
            strike=strike,
            maturity=MATURITY,
            contract_multiplier=10_000,
            price_tick=0.0001,
        )
        for strike in (3.15, 3.3, 3.45)
        for option_type in (OptionType.CALL, OptionType.PUT)
    )
    universe = StrategyUniverse(
        underlying="UNDERLYING",
        maturity=MATURITY,
        center_strike=3.3,
        strikes=(3.15, 3.3, 3.45),
        contracts=contracts,
    )
    state = MarketState(["UNDERLYING", *universe.instruments])
    state.apply_quote(_quote("UNDERLYING", last=3.3, bid=3.299, ask=3.301))

    expiry = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)
    time_to_expiry = (expiry - NOW).total_seconds() / (365 * 86400)
    rate = 0.015
    forward = 3.3 * math.exp(rate * time_to_expiry)
    parameters = SabrParameters(alpha=0.35, beta=0.5, nu=0.8, rho=-0.3)
    for contract in contracts:
        volatility = sabr_volatility(
            forward,
            contract.strike,
            time_to_expiry,
            parameters,
        )
        price = black76_price(
            forward,
            contract.strike,
            time_to_expiry,
            rate,
            volatility,
            contract.option_type.value,
        )
        spread = min(price * 0.01, 0.00001)
        state.apply_quote(
            _quote(
                contract.instrument,
                last=price,
                bid=price - spread,
                ask=price + spread,
            )
        )
    return state, universe


def _quote(
    instrument: str,
    *,
    last: float,
    bid: float,
    ask: float,
) -> MarketQuote:
    return MarketQuote(
        instrument=instrument,
        observed_at=datetime(2026, 9, 17, 10, 30),
        received_at=NOW,
        trading_date=NOW.date(),
        last=last,
        bid=bid,
        ask=ask,
    )


if __name__ == "__main__":
    unittest.main()
