from datetime import date, datetime, timezone
import unittest

from sim_hedge.market.alpha_market import build_alpha_market_snapshot
from sim_hedge.domain.contract import OptionContract, OptionType
from sim_hedge.domain.quote import MarketQuote
from sim_hedge.market.state import MarketState


NOW = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)


class AlphaMarketTests(unittest.TestCase):
    def test_captures_the_full_nearest_chain_with_margin_inputs(self) -> None:
        contracts = [
            OptionContract(
                instrument=f"{kind.value}{strike}",
                underlying="ETF",
                option_type=kind,
                strike=strike,
                maturity=maturity,
                contract_multiplier=10000,
                price_tick=0.0001,
            )
            for maturity in (date(2026, 9, 23), date(2026, 10, 28))
            for strike in (3.2, 3.4)
            for kind in (OptionType.CALL, OptionType.PUT)
        ]
        state = MarketState(["ETF"])
        state.apply_quote(_quote("ETF", 3.3, previous_close=3.29))
        for contract in contracts:
            state.apply_quote(
                _quote(contract.instrument, 0.1, previous_settlement=0.09)
            )

        result = build_alpha_market_snapshot(
            snapshot_id="M1",
            as_of=NOW,
            underlying="ETF",
            contracts=contracts,
            market_state=state,
        )

        self.assertEqual(result["underlying"]["previousClose"], 3.29)
        self.assertEqual(len(result["options"]), 4)
        self.assertEqual(
            {option["previousSettlement"] for option in result["options"]},
            {0.09},
        )


def _quote(
    instrument: str,
    price: float,
    *,
    previous_close: float | None = None,
    previous_settlement: float | None = None,
) -> MarketQuote:
    return MarketQuote(
        instrument=instrument,
        observed_at=NOW.replace(tzinfo=None),
        received_at=NOW,
        trading_date=NOW.date(),
        last=price,
        bid=price - 0.001,
        ask=price + 0.001,
        previous_close=previous_close,
        previous_settlement=previous_settlement,
    )


if __name__ == "__main__":
    unittest.main()
