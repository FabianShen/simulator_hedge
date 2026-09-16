import unittest

from sim_hedge.domain import MarketTick
from sim_hedge.market import generate_market_ticks


class GenerateMarketTicksTests(unittest.TestCase):
    def test_builds_expected_price_path(self) -> None:
        ticks = list(generate_market_ticks(100.0, (1.0, -0.5, 2.0)))

        self.assertEqual(
            ticks,
            [
                MarketTick(step=0, spot=100.0),
                MarketTick(step=1, spot=101.0),
                MarketTick(step=2, spot=100.5),
                MarketTick(step=3, spot=102.5),
            ],
        )

    def test_rejects_a_non_positive_price(self) -> None:
        with self.assertRaisesRegex(ValueError, "spot must be positive"):
            list(generate_market_ticks(1.0, (-1.0,)))


if __name__ == "__main__":
    unittest.main()

