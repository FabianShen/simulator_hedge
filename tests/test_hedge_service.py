import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf import json_format

from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine
from hedge_service.protobuf_codec import request_from_proto
from hedging.v1 import hedging_pb2


REQUEST_PATH = (
    Path(__file__).parents[1]
    / "protocols"
    / "hedging"
    / "v1"
    / "examples"
    / "hedge_request.json"
)
RESPONSE_PATH = REQUEST_PATH.with_name("hedge_response.json")
NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)

def make_request(
    *,
    alpha_positions=None,
    beta_positions=None,
    hedge_universe=("CALL", "PUT"),
) -> HedgeRequest:
    return HedgeRequest(
        request_id="hedge-test",
        source_pricing_request_id="pricing-test",
        market_as_of=NOW,
        account_id="ACCOUNT",
        base_ledger_revision=1,
        spot=100.0,
        instruments=(
            HedgeInstrument(
                instrument="ALPHA",
                option_type="CALL",
                strike=105.0,
                contract_multiplier=1,
                delta=2.0,
                gamma=4.0,
                theta=0.0,
                vega=0.0,
            ),
            HedgeInstrument(
                instrument="CALL",
                option_type="CALL",
                strike=100.0,
                contract_multiplier=1,
                delta=1.0,
                gamma=1.0,
                theta=0.0,
                vega=0.0,
            ),
            HedgeInstrument(
                instrument="PUT",
                option_type="PUT",
                strike=100.0,
                contract_multiplier=1,
                delta=-1.0,
                gamma=1.0,
                theta=0.0,
                vega=0.0,
            ),
        ),
        confirmed_alpha_positions=(
            {"ALPHA": -1}
            if alpha_positions is None
            else alpha_positions
        ),
        confirmed_beta_positions=(
            {}
            if beta_positions is None
            else beta_positions
        ),
        hedge_universe=tuple(hedge_universe),
    )

class ReferenceHedgeServiceTests(unittest.TestCase):
    def test_protocol_examples_have_matching_identifiers(self) -> None:
        request = hedging_pb2.HedgeRequest()
        response = hedging_pb2.HedgeProposal()
        json_format.Parse(REQUEST_PATH.read_text(encoding="utf-8"), request)
        json_format.Parse(RESPONSE_PATH.read_text(encoding="utf-8"), response)

        self.assertEqual(response.request_id, request.request_id)
        self.assertEqual(
            response.source_pricing_request_id, request.source_pricing_request_id
        )
        self.assertEqual(response.source_market_as_of, request.market_as_of)
        self.assertEqual(
            response.base_strategy_ledger_revision,
            request.base_strategy_ledger_revision,
        )
        self.assertFalse(response.orders_generated)

    def test_recorded_request_produces_an_incremental_proposal(self) -> None:
        message = hedging_pb2.HedgeRequest()
        json_format.Parse(REQUEST_PATH.read_text(encoding="utf-8"), message)

        result = ReferenceHedgeEngine().propose(
            request_from_proto(message), created_at=NOW
        )

        self.assertEqual(result.hedge_pair, ("CALL", "PUT"))
        self.assertEqual(
            result.proposal["incremental_trades"], {"CALL": 3, "PUT": 1}
        )
        self.assertEqual(
            result.proposal["target_beta_positions"], {"CALL": 3, "PUT": 1}
        )
        self.assertFalse(result.proposal["orders_generated"])

    def test_missing_held_instrument_greeks_are_rejected(self) -> None:
        payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
        payload["instruments"] = [
            item for item in payload["instruments"] if item["instrument"] != "ALPHA"
        ]
        message = hedging_pb2.HedgeRequest()
        json_format.ParseDict(payload, message)

        with self.assertRaisesRegex(ValueError, "held positions.*ALPHA"):
            ReferenceHedgeEngine().propose(
                request_from_proto(message), created_at=NOW
            )
    def test_default_relative_limits_trigger_delta_gamma_hedge(self) -> None:
        engine = ReferenceHedgeEngine()
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        # Alpha position = -1
        #
        # Alpha Delta = -2
        # gross Alpha Delta scale = 2
        # default Delta limit = 15% * 2 = 0.3
        #
        # Alpha Gamma = -4
        # Alpha Gamma scale = 4
        # default Gamma limit = 30% * 4 = 1.2
        #
        # Both risks breach the default bands.
        self.assertEqual(
            result.proposal["incremental_trades"],
            {"CALL": 3, "PUT": 1},
        )
        self.assertEqual(result.hedge_pair, ("CALL", "PUT"))


    def test_manual_limits_override_relative_limits(self) -> None:
        engine = ReferenceHedgeEngine(
            delta_limit=10.0,
            gamma_limit=10.0,
        )
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        # Current portfolio Delta = -2
        # Current portfolio Gamma = -4
        #
        # Both are inside manually supplied limits.
        self.assertEqual(result.proposal["incremental_trades"], {})
        self.assertEqual(result.hedge_pair, ())
        self.assertEqual(result.risk_at_target_beta, result.portfolio_risk)


    def test_manual_delta_limit_keeps_relative_gamma_limit(self) -> None:
        engine = ReferenceHedgeEngine(
            delta_limit=10.0,
        )
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        # Delta is safe under manual limit:
        # |-2| < 10
        #
        # Gamma still uses the default relative limit:
        # limit = 30% * 4 = 1.2
        # |-4| > 1.2
        #
        # Therefore the same D/G hedge should run.
        self.assertEqual(
            result.proposal["incremental_trades"],
            {"CALL": 3, "PUT": 1},
        )


    def test_manual_gamma_limit_keeps_relative_delta_limit(self) -> None:
        engine = ReferenceHedgeEngine(
            gamma_limit=10.0,
        )
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        # Gamma is safe under manual limit:
        # |-4| < 10
        #
        # Delta still uses:
        # 15% * gross Alpha Delta = 0.3
        #
        # |-2| > 0.3
        self.assertEqual(
            result.proposal["incremental_trades"],
            {"CALL": 3, "PUT": 1},
        )


    def test_risk_exactly_on_limits_does_not_trigger_hedge(self) -> None:
        engine = ReferenceHedgeEngine(
            delta_limit=2.0,
            gamma_limit=4.0,
        )
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        # Breach condition uses ">", not ">=".
        self.assertEqual(result.proposal["incremental_trades"], {})
        self.assertEqual(result.hedge_pair, ())


    def test_zero_limits_trigger_on_any_nonzero_risk(self) -> None:
        engine = ReferenceHedgeEngine(
            delta_limit=0.0,
            gamma_limit=0.0,
        )
        request = make_request()

        result = engine.propose(request, created_at=NOW)

        self.assertEqual(
            result.proposal["incremental_trades"],
            {"CALL": 3, "PUT": 1},
        )


    def test_empty_alpha_and_beta_require_no_hedge(self) -> None:
        engine = ReferenceHedgeEngine()
        request = make_request(
            alpha_positions={},
            beta_positions={},
        )

        result = engine.propose(request, created_at=NOW)

        self.assertEqual(result.portfolio_risk.delta, 0.0)
        self.assertEqual(result.portfolio_risk.gamma, 0.0)
        self.assertEqual(result.proposal["incremental_trades"], {})
        self.assertEqual(result.hedge_pair, ())


    def test_existing_beta_can_put_portfolio_inside_relative_bands(self) -> None:
        engine = ReferenceHedgeEngine()

        request = make_request(
            beta_positions={
                "CALL": 3,
                "PUT": 1,
            },
        )

        result = engine.propose(request, created_at=NOW)

        # Alpha:
        #   Delta = -2
        #   Gamma = -4
        #
        # Beta:
        #   3 CALL + 1 PUT
        #   Delta = 3 - 1 = +2
        #   Gamma = 3 + 1 = +4
        #
        # Portfolio is exactly neutral.
        self.assertAlmostEqual(result.portfolio_risk.delta, 0.0)
        self.assertAlmostEqual(result.portfolio_risk.gamma, 0.0)

        self.assertEqual(result.proposal["incremental_trades"], {})
        self.assertEqual(result.hedge_pair, ())


    def test_safe_portfolio_does_not_require_valid_hedge_pair(self) -> None:
        engine = ReferenceHedgeEngine()

        request = make_request(
            alpha_positions={},
            beta_positions={},
            hedge_universe=("CALL",),
        )

        # There is no usable CALL/PUT pair.
        #
        # But because portfolio risk is already safe,
        # _select_pair() should never be called.
        result = engine.propose(request, created_at=NOW)

        self.assertEqual(result.proposal["incremental_trades"], {})
        self.assertEqual(result.hedge_pair, ())


    def test_breached_portfolio_requires_valid_hedge_pair(self) -> None:
        engine = ReferenceHedgeEngine()

        request = make_request(
            hedge_universe=("CALL",),
        )

        # Risk is breached, therefore D/G hedging is required.
        # But only a CALL is available, so no valid D/G pair exists.
        with self.assertRaisesRegex(
            ValueError,
            "no non-singular call/put pair",
        ):
            engine.propose(request, created_at=NOW)



if __name__ == "__main__":
    unittest.main()
