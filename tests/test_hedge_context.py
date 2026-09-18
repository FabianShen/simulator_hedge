from datetime import datetime, timedelta, timezone
import unittest

from hedge_engine import (
    Greeks,
    InstrumentGreeks,
    MarketSnapshot,
    build_hedge_context,
)


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


class HedgeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.greeks = {
            code: InstrumentGreeks(code, Greeks(delta=index, gamma=1))
            for index, code in enumerate(("ALPHA", "CALL", "PUT"), start=1)
        }
        self.market = MarketSnapshot(
            as_of=NOW,
            spot=3.4,
            spot_observed_at=NOW,
            marks={"ALPHA": 0.1, "CALL": 0.2, "PUT": 0.3},
            observed_at={code: NOW for code in ("ALPHA", "CALL", "PUT")},
        )

    def test_reconciles_books_and_excludes_alpha_from_hedge_universe(self) -> None:
        context = build_hedge_context(
            market=self.market,
            alpha_positions={"ALPHA": -2},
            beta_positions={"CALL": 1},
            broker_positions={"ALPHA": -2, "CALL": 1},
            strategy_universe=("ALPHA", "CALL", "PUT"),
            instrument_greeks=self.greeks,
        )

        self.assertEqual(context.hedge_universe, ("CALL", "PUT"))
        self.assertNotIn("ALPHA", context.hedge_universe)
        self.assertIn("ALPHA", context.instrument_greeks)

    def test_rejects_unreconciled_broker_positions(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            build_hedge_context(
                market=self.market,
                alpha_positions={"ALPHA": -2},
                beta_positions={},
                broker_positions={"ALPHA": -1},
                strategy_universe=("ALPHA", "CALL", "PUT"),
                instrument_greeks=self.greeks,
            )

    def test_rejects_alpha_beta_instrument_overlap(self) -> None:
        with self.assertRaisesRegex(ValueError, "disjoint"):
            build_hedge_context(
                market=self.market,
                alpha_positions={"ALPHA": -2},
                beta_positions={"ALPHA": 1},
                broker_positions={"ALPHA": -1},
                strategy_universe=("ALPHA", "CALL", "PUT"),
                instrument_greeks=self.greeks,
            )

    def test_rejects_stale_candidate_market_data(self) -> None:
        stale_market = MarketSnapshot(
            as_of=NOW,
            spot=3.4,
            spot_observed_at=NOW,
            marks=self.market.marks,
            observed_at={
                **self.market.observed_at,
                "PUT": NOW - timedelta(seconds=11),
            },
        )
        with self.assertRaisesRegex(ValueError, "stale.*PUT"):
            build_hedge_context(
                market=stale_market,
                alpha_positions={"ALPHA": -2},
                beta_positions={},
                broker_positions={"ALPHA": -2},
                strategy_universe=("ALPHA", "CALL", "PUT"),
                instrument_greeks=self.greeks,
            )


if __name__ == "__main__":
    unittest.main()
