import unittest
from datetime import datetime, timezone
from decimal import Decimal

from hedge_engine import ConfirmedFill, apply_confirmed_fills, empty_ledger
from sim_hedge.hedge_request import build_hedge_request
from sim_hedge.pricing_types import OptionValuation, PricingBatch, SabrFit


NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class HedgeRequestBuilderTests(unittest.TestCase):
    def test_explicit_hedge_candidates_do_not_include_valuation_only_options(self) -> None:
        request = _pricing_request()
        request["options"].append(_option("HELD-FAR", "OPTION_TYPE_CALL", 3.9))
        result = _pricing_result()
        result = PricingBatch(
            request_id=result.request_id, calculated_at=result.calculated_at,
            engine_name=result.engine_name, engine_version=result.engine_version,
            model=result.model, calibration=result.calibration,
            results=(*result.results, _valuation("HELD-FAR", 0.1, 0.2)),
        )
        hedge = build_hedge_request(
            request, result, _ledger(), hedge_candidates=("CALL", "PUT"),
        )
        self.assertIn("HELD-FAR", {item["instrument"] for item in hedge["instruments"]})
        self.assertEqual(hedge["hedgeUniverse"], ["CALL", "PUT"])

    def test_carries_exact_pricing_snapshot_and_confirmed_ledger(self) -> None:
        request = build_hedge_request(_pricing_request(), _pricing_result(), _ledger())

        self.assertEqual(request["sourcePricingRequestId"], "pricing-1")
        self.assertEqual(request["marketAsOf"], "2026-09-21T03:00:00Z")
        self.assertEqual(request["baseStrategyLedgerRevision"], 1)
        self.assertEqual(request["confirmedAlphaPositions"], {"ALPHA": -1})
        self.assertEqual(request["confirmedBetaPositions"], {})
        self.assertEqual(request["hedgeUniverse"], ["CALL", "PUT"])
        self.assertEqual(
            {item["instrument"] for item in request["instruments"]},
            {"ALPHA", "CALL", "PUT"},
        )

    def test_rejects_invalid_pricing_for_a_held_position(self) -> None:
        result = _pricing_result(
            alpha=OptionValuation(
                instrument="ALPHA",
                status="INVALID_INPUT",
                error="bad market",
                theoretical_price=None,
                market_implied_volatility=None,
                model_implied_volatility=None,
                implied_volatility_error=None,
                delta=None,
                gamma=None,
                theta_per_year=None,
                vega_per_absolute_volatility=None,
                rho_per_absolute_rate=None,
            )
        )

        with self.assertRaisesRegex(ValueError, "held positions.*ALPHA"):
            build_hedge_request(_pricing_request(), result, _ledger())


def _ledger():
    fill = ConfirmedFill(
        trade_id="T1",
        order_id="O1",
        account_id="A1",
        strategy="ALPHA",
        instrument="ALPHA",
        quantity=-1,
        price=Decimal("0.1"),
        executed_at=NOW,
    )
    return apply_confirmed_fills(empty_ledger("A1"), (fill,))


def _pricing_request():
    return {
        "requestId": "pricing-1",
        "asOf": "2026-09-21T03:00:00Z",
        "underlying": {"instrument": "ETF", "spot": 3.3},
        "options": [
            _option("ALPHA", "OPTION_TYPE_CALL", 3.4),
            _option("CALL", "OPTION_TYPE_CALL", 3.3),
            _option("PUT", "OPTION_TYPE_PUT", 3.3),
        ],
    }


def _option(instrument, option_type, strike):
    return {
        "instrument": instrument,
        "optionType": option_type,
        "strike": strike,
        "contractMultiplier": 1,
    }


def _pricing_result(alpha=None):
    return PricingBatch(
        request_id="pricing-1",
        calculated_at=NOW,
        engine_name="pricing",
        engine_version="1",
        model="SABR_BLACK_76",
        calibration=SabrFit(3.3, 0.3, 0.5, 0.8, -0.2, 0.001, 3),
        results=(
            alpha or _valuation("ALPHA", 2, 4),
            _valuation("CALL", 1, 1),
            _valuation("PUT", -1, 1),
        ),
    )


def _valuation(instrument, delta, gamma):
    return OptionValuation(
        instrument=instrument,
        status="OK",
        error=None,
        theoretical_price=0.1,
        market_implied_volatility=0.2,
        model_implied_volatility=0.2,
        implied_volatility_error=0,
        delta=delta,
        gamma=gamma,
        theta_per_year=-0.1,
        vega_per_absolute_volatility=0.2,
        rho_per_absolute_rate=0.1,
    )


if __name__ == "__main__":
    unittest.main()
