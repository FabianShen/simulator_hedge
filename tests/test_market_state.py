import unittest
from datetime import date, datetime, timedelta, timezone

from sim_hedge.domain.quote import MarketQuote
from sim_hedge.market.state import MarketState


NOW = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)


def quote(instrument: str, received_at: datetime = NOW) -> MarketQuote:
    return MarketQuote(
        instrument=instrument,
        observed_at=datetime(2026, 9, 17, 10, 30),
        received_at=received_at,
        trading_date=date(2026, 9, 17),
        last=3.34,
        bid=3.339,
        ask=3.340,
    )


class MarketStateTests(unittest.TestCase):
    def test_required_instruments_can_change_after_universe_selection(self) -> None:
        state = MarketState(["UNDERLYING"])

        state.set_required(["UNDERLYING", "CALL", "PUT"])

        self.assertEqual(
            state.required_instruments,
            ("UNDERLYING", "CALL", "PUT"),
        )

    def test_is_not_ready_while_a_required_quote_is_missing(self) -> None:
        state = MarketState(["UNDERLYING", "OPTION"])
        state.apply_quote(quote("UNDERLYING"))

        result = state.readiness(now=NOW, max_age=timedelta(seconds=5))

        self.assertFalse(result.ready)
        self.assertEqual(result.missing, ("OPTION",))
        self.assertEqual(result.stale, ())

    def test_is_ready_when_all_quotes_are_present_and_fresh(self) -> None:
        state = MarketState(["UNDERLYING", "OPTION"])
        state.apply_quote(quote("UNDERLYING"))
        state.apply_quote(quote("OPTION"))

        result = state.readiness(now=NOW, max_age=timedelta(seconds=5))

        self.assertTrue(result.ready)

    def test_reports_stale_quotes(self) -> None:
        state = MarketState(["UNDERLYING"])
        state.apply_quote(quote("UNDERLYING", NOW - timedelta(seconds=6)))

        result = state.readiness(now=NOW, max_age=timedelta(seconds=5))

        self.assertFalse(result.ready)
        self.assertEqual(result.stale, ("UNDERLYING",))

    def test_unsafe_feed_prevents_readiness(self) -> None:
        state = MarketState(["UNDERLYING"])
        state.apply_quote(quote("UNDERLYING"))

        result = state.readiness(
            now=NOW,
            max_age=timedelta(seconds=5),
            feed_unsafe=True,
        )

        self.assertFalse(result.ready)
        self.assertTrue(result.feed_unsafe)

    def test_snapshot_is_detached_from_internal_state(self) -> None:
        state = MarketState(["UNDERLYING"])
        state.apply_quote(quote("UNDERLYING"))

        snapshot = state.snapshot()
        snapshot.clear()

        self.assertIsNotNone(state.latest("UNDERLYING"))


if __name__ == "__main__":
    unittest.main()
