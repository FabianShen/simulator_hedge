import math
import unittest

from pricing_engine import (
    OptionObservation,
    SabrParameters,
    SabrPricingEngine,
    SabrPricingRequest,
)
from pricing_engine.black76 import black76_price, implied_volatility
from pricing_engine.sabr import sabr_volatility


class Black76Tests(unittest.TestCase):
    def test_call_put_parity(self) -> None:
        forward = 100.0
        strike = 105.0
        time = 0.5
        rate = 0.02
        volatility = 0.25

        call = black76_price(forward, strike, time, rate, volatility, "CALL")
        put = black76_price(forward, strike, time, rate, volatility, "PUT")

        expected = math.exp(-rate * time) * (forward - strike)
        self.assertAlmostEqual(call - put, expected, places=12)

    def test_implied_volatility_recovers_price_input(self) -> None:
        price = black76_price(100.0, 95.0, 0.25, 0.01, 0.31, "PUT")

        solved = implied_volatility(100.0, 95.0, 0.25, 0.01, price, "PUT")

        self.assertAlmostEqual(solved, 0.31, places=7)


class SabrVolatilityTests(unittest.TestCase):
    def test_smile_is_positive_and_changes_by_strike(self) -> None:
        parameters = SabrParameters(alpha=0.35, beta=0.5, nu=0.8, rho=-0.3)

        values = [
            sabr_volatility(3.3, strike, 30 / 365, parameters)
            for strike in (3.0, 3.3, 3.6)
        ]

        self.assertTrue(all(value > 0 for value in values))
        self.assertGreater(max(values) - min(values), 0.001)


class SabrPricingEngineTests(unittest.TestCase):
    def test_prices_saved_observations_without_live_data(self) -> None:
        request = _offline_request()
        engine = SabrPricingEngine()

        response = engine.price(request)

        self.assertEqual(response.calibration.valid_strikes, 5)
        self.assertEqual(len(response.results), 10)
        self.assertTrue(all(result.status == "OK" for result in response.results))
        self.assertTrue(
            all(result.theoretical_price > 0 for result in response.results)
        )
        call = next(result for result in response.results if result.instrument == "C-3.3")
        put = next(result for result in response.results if result.instrument == "P-3.3")
        self.assertGreater(call.delta, 0)
        self.assertLess(put.delta, 0)

    def test_same_request_is_deterministic(self) -> None:
        request = _offline_request()
        engine = SabrPricingEngine()

        first = engine.price(request)
        second = engine.price(request)

        self.assertEqual(first, second)


def _offline_request() -> SabrPricingRequest:
    spot = 3.3
    time = 30 / 365
    rate = 0.015
    dividend = 0.0
    forward = spot * math.exp((rate - dividend) * time)
    parameters = SabrParameters(alpha=0.35, beta=0.5, nu=0.8, rho=-0.3)
    observations = []
    for strike in (3.0, 3.15, 3.3, 3.45, 3.6):
        volatility = sabr_volatility(forward, strike, time, parameters)
        for prefix, option_type in (("C", "CALL"), ("P", "PUT")):
            market_price = black76_price(
                forward, strike, time, rate, volatility, option_type
            )
            observations.append(
                OptionObservation(
                    instrument=f"{prefix}-{strike}",
                    option_type=option_type,
                    strike=strike,
                    market_price=market_price,
                )
            )
    return SabrPricingRequest(
        spot=spot,
        time_to_expiry=time,
        rate=rate,
        dividend_yield=dividend,
        options=tuple(observations),
        beta=0.5,
    )


if __name__ == "__main__":
    unittest.main()
