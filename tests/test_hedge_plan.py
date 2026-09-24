import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hedge_engine import validate_hedge_proposal
from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine

from pricing_engine import OptionPricingResult, SabrPricingEngine
from pricing_engine.__main__ import load_request
from sim_hedge.hedge_client.plan import _usable_instrument_greeks, build_offline_hedge_decision, main


AS_OF = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
DELTA_LIMIT = 1.0
GAMMA_LIMIT = 1.0
SABR_EXAMPLE = Path(__file__).parents[1] / "pricing_engine" / "examples" / "sabr_request.json"


def valid(instrument: str) -> OptionPricingResult:
    return OptionPricingResult(
        instrument=instrument,
        status="OK",
        delta=0.5,
        gamma=0.2,
        theta_per_year=-0.1,
        vega_per_absolute_volatility=0.3,
    )


def invalid(instrument: str) -> OptionPricingResult:
    return OptionPricingResult(
        instrument=instrument,
        status="INVALID_INPUT",
        error="market_price is outside no-arbitrage bounds",
    )


class HedgePlanPricingFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "HELD": {"contractMultiplier": 10_000},
            "CANDIDATE": {"contractMultiplier": 10_000},
            "INVALID": {"contractMultiplier": 10_000},
        }

    def test_excludes_invalid_unheld_contract_and_retains_reason(self) -> None:
        greeks, exclusions = _usable_instrument_greeks(
            self.metadata,
            {
                "HELD": valid("HELD"),
                "CANDIDATE": valid("CANDIDATE"),
                "INVALID": invalid("INVALID"),
            },
            {"HELD"},
        )

        self.assertEqual(set(greeks), {"HELD", "CANDIDATE"})
        self.assertEqual(
            exclusions["INVALID"],
            {
                "status": "INVALID_INPUT",
                "reason": "market_price is outside no-arbitrage bounds",
            },
        )

    def test_rejects_invalid_contract_when_it_is_held(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "valid pricing Greeks missing for held positions.*INVALID"
        ):
            _usable_instrument_greeks(
                self.metadata,
                {
                    "HELD": valid("HELD"),
                    "CANDIDATE": valid("CANDIDATE"),
                    "INVALID": invalid("INVALID"),
                },
                {"HELD", "INVALID"},
            )

    def test_missing_pricing_result_is_an_exclusion(self) -> None:
        _, exclusions = _usable_instrument_greeks(
            self.metadata,
            {"HELD": valid("HELD"), "CANDIDATE": valid("CANDIDATE")},
            {"HELD"},
        )

        self.assertEqual(exclusions["INVALID"]["status"], "MISSING")


class HedgePlanReplayTests(unittest.TestCase):
    def test_breached_risk_uses_reference_engine_pair_and_trade(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": -1000}
        with tempfile.TemporaryDirectory() as directory:
            path = _write_pricing(Path(directory), pricing)
            result, context, exclusions = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha), _ledger(alpha),
                delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
            )
            response = SabrPricingEngine().price(load_request(path))
        results = {item.instrument: item for item in response.results}
        expected_request = HedgeRequest(
            request_id="hedge-pricing-replay-ledger-1",
            source_pricing_request_id="pricing-replay",
            market_as_of=AS_OF,
            account_id="ACCOUNT",
            base_ledger_revision=1,
            spot=3.3,
            instruments=tuple(
                HedgeInstrument(
                    instrument=item["instrument"],
                    option_type=item["optionType"].removeprefix("OPTION_TYPE_"),
                    strike=item["strike"],
                    contract_multiplier=1,
                    delta=results[item["instrument"]].delta,
                    gamma=results[item["instrument"]].gamma,
                    theta=results[item["instrument"]].theta_per_year,
                    vega=results[item["instrument"]].vega_per_absolute_volatility,
                )
                for item in pricing["options"]
            ),
            confirmed_alpha_positions=alpha,
            confirmed_beta_positions={},
            hedge_universe=context.hedge_universe,
        )
        expected = ReferenceHedgeEngine(
            delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
        ).propose(expected_request, created_at=AS_OF)
        self.assertEqual(exclusions, {})
        self.assertEqual(result.hedge_pair, expected.hedge_pair)
        self.assertEqual(
            result.proposal["incremental_trades"],
            expected.proposal["incremental_trades"],
        )
        self.assertEqual(
            result.proposal["target_beta_positions"],
            expected.proposal["target_beta_positions"],
        )
        self.assertTrue(result.proposal["incremental_trades"])
        self.assertEqual(result.proposal["decision_engine"]["name"], "reference-python-hedge")

    def test_inside_bands_with_existing_beta_requires_no_new_hedge(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": -1000}
        with tempfile.TemporaryDirectory() as directory:
            path = _write_pricing(Path(directory), pricing)
            first, _, _ = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha), _ledger(alpha),
                delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
            )
            beta = first.proposal["target_beta_positions"]
            inside, _, _ = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha, beta), _ledger(alpha, beta),
                delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
            )
        self.assertTrue(beta)
        self.assertEqual(inside.hedge_pair, ())
        self.assertEqual(inside.proposal["incremental_trades"], {})
        self.assertEqual(inside.proposal["target_beta_positions"], beta)

    def test_invalid_unheld_candidate_is_excluded(self) -> None:
        pricing = _recorded_pricing()
        next(item for item in pricing["options"] if item["instrument"] == "C-3.6")["marketPrice"] = 999.0
        result, context, exclusions = _decision(pricing)
        self.assertIn("C-3.6", exclusions)
        self.assertNotIn("C-3.6", context.hedge_universe)
        self.assertNotIn("C-3.6", result.hedge_pair)

    def test_invalid_held_greeks_block_planning(self) -> None:
        pricing = _recorded_pricing()
        next(item for item in pricing["options"] if item["instrument"] == "C-3.45")["marketPrice"] = 999.0
        with self.assertRaisesRegex(ValueError, "valid pricing Greeks missing for held positions"):
            _decision(pricing)

    def test_valuation_only_held_beta_contributes_risk_but_is_not_a_candidate(self) -> None:
        pricing = _recorded_pricing()
        held_beta = "C-3.6"
        held_option = next(
            item for item in pricing["options"]
            if item["instrument"] == held_beta
        )
        held_option.pop("marketPrice")
        held_option.pop("observedAt")
        alpha = {"C-3.45": -1000}
        beta = {held_beta: 7}
        with tempfile.TemporaryDirectory() as directory:
            path = _write_pricing(Path(directory), pricing)
            response = SabrPricingEngine().price(load_request(path))
            result, context, exclusions = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha, beta), _ledger(alpha, beta),
                delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
            )
        priced = {item.instrument: item for item in response.results}[held_beta]

        self.assertEqual(exclusions, {})
        self.assertIn(held_beta, context.instrument_greeks)
        self.assertNotIn(held_beta, context.hedge_universe)
        self.assertAlmostEqual(result.confirmed_beta_risk.delta, 7 * priced.delta)
        self.assertAlmostEqual(result.confirmed_beta_risk.gamma, 7 * priced.gamma)
        self.assertNotIn(held_beta, result.proposal["incremental_trades"])

    def test_valuation_only_held_alpha_replays_without_becoming_a_candidate(self) -> None:
        pricing = _recorded_pricing()
        held_alpha = "C-3.45"
        held_option = next(
            item for item in pricing["options"]
            if item["instrument"] == held_alpha
        )
        held_option.pop("marketPrice")
        held_option.pop("observedAt")

        result, context, exclusions = _decision(pricing)

        self.assertEqual(exclusions, {})
        self.assertIn(held_alpha, context.instrument_greeks)
        self.assertNotIn(held_alpha, context.hedge_universe)
        self.assertNotEqual(result.alpha_risk.delta, 0)

    def test_active_broker_orders_block_planning(self) -> None:
        portfolio = _portfolio({"C-3.45": -1000})
        portfolio["active_orders"] = [{"order_id": "O1"}]
        with self.assertRaisesRegex(ValueError, "broker orders are active"):
            _decision(_recorded_pricing(), portfolio=portfolio)

    def test_broker_ledger_mismatch_blocks_planning(self) -> None:
        with self.assertRaisesRegex(ValueError, "broker positions do not reconcile"):
            _decision(_recorded_pricing(), portfolio=_portfolio({}))

    def test_stale_option_observation_blocks_planning(self) -> None:
        pricing = _recorded_pricing()
        pricing["options"][0]["observedAt"] = (AS_OF - timedelta(seconds=30)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale market observation"):
            _decision(pricing, max_market_age_seconds=10.0)

    def test_unconfirmed_ledger_blocks_planning(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": -1000}
        ledger = _ledger(alpha)
        ledger["status"] = "NOT_READY"
        with tempfile.TemporaryDirectory() as directory:
            path = _write_pricing(Path(directory), pricing)
            with self.assertRaisesRegex(ValueError, "strategy ledger must contain confirmed fills"):
                build_offline_hedge_decision(
                    path, pricing, _portfolio(alpha), ledger,
                    delta_limit=DELTA_LIMIT, gamma_limit=GAMMA_LIMIT,
                )

    def test_portfolio_account_must_match_ledger(self) -> None:
        pricing = _recorded_pricing()
        portfolio = _portfolio({"C-3.45": -1000})
        portfolio["account"]["account_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "account IDs do not match"):
            _decision(pricing, portfolio=portfolio)

    def test_held_instrument_must_be_in_pricing_universe(self) -> None:
        pricing = _recorded_pricing()
        pricing["options"] = [
            item for item in pricing["options"] if item["instrument"] != "C-3.45"
        ]
        with self.assertRaisesRegex(ValueError, "held positions are outside"):
            _decision(pricing)

    def test_cli_writes_canonical_proposal_with_offline_diagnostics(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": -1000}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pricing_path = _write_pricing(root, pricing)
            portfolio_path = root / "portfolio.json"
            ledger_path = root / "ledger.json"
            output_path = root / "hedge_plan.json"
            portfolio_path.write_text(json.dumps(_portfolio(alpha)), encoding="utf-8")
            ledger_path.write_text(json.dumps(_ledger(alpha)), encoding="utf-8")
            with patch.object(sys, "argv", [
                "hedge_plan", str(pricing_path), str(portfolio_path),
                str(ledger_path), "--output", str(output_path),
                "--delta-limit", str(DELTA_LIMIT),
                "--gamma-limit", str(GAMMA_LIMIT),
                "--target-delta", "5000",
                "--target-gamma", "0",
            ]), contextlib.redirect_stdout(io.StringIO()):
                main()
            output = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            validate_hedge_proposal(
                output, pricing_request_id="pricing-replay", account_id="ACCOUNT",
                base_ledger_revision=1, confirmed_beta_positions={},
            ),
            output["incremental_trades"],
        )
        self.assertEqual(output["decision_engine"]["name"], "reference-python-hedge")
        self.assertEqual(output["source_market_as_of"], "2026-09-21T03:00:00Z")
        self.assertIn("risk_at_target_beta", output)
        self.assertLess(
            abs(output["risk_at_target_beta"]["delta"] - 5000),
            abs(output["risk"]["before_hedge"]["delta"] - 5000),
        )
        self.assertIn("pricing_exclusions", output)
        self.assertFalse(output["orders_generated"])


def _decision(pricing, *, portfolio=None, max_market_age_seconds=10.0):
    alpha = {"C-3.45": -1000}
    with tempfile.TemporaryDirectory() as directory:
        path = _write_pricing(Path(directory), pricing)
        return build_offline_hedge_decision(
            path, pricing, portfolio or _portfolio(alpha), _ledger(alpha),
            max_market_age_seconds=max_market_age_seconds,
            delta_limit=DELTA_LIMIT,
            gamma_limit=GAMMA_LIMIT,
        )


def _write_pricing(directory: Path, pricing: dict) -> Path:
    path = directory / "pricing.json"
    path.write_text(json.dumps(pricing), encoding="utf-8")
    return path


def _recorded_pricing() -> dict:
    example = json.loads(SABR_EXAMPLE.read_text(encoding="utf-8"))
    expiry = (AS_OF + timedelta(days=30)).isoformat()
    return {
        "requestId": "pricing-replay",
        "asOf": AS_OF.isoformat().replace("+00:00", "Z"),
        "underlying": {"instrument": "ETF", "spot": 3.3, "observedAt": AS_OF.isoformat()},
        "options": [
            {
                "instrument": item["instrument"],
                "optionType": f"OPTION_TYPE_{item['option_type']}",
                "strike": item["strike"],
                "expiry": expiry,
                "observedAt": AS_OF.isoformat(),
                "contractMultiplier": 1,
                "marketPrice": item["market_price"],
            }
            for item in example["options"]
        ],
        "assumptions": {
            "riskFreeRate": example["rate"], "dividendYield": example["dividend_yield"],
            "dayCount": "DAY_COUNT_ACT_365_FIXED",
        },
        "configuration": {
            "model": "PRICING_MODEL_SABR_BLACK_76",
            "calculateImpliedVolatility": True,
            "sabr": {"beta": example["beta"], "minimumStrikes": 3},
        },
    }


def _ledger(alpha, beta=None) -> dict:
    return {
        "status": "CONFIRMED", "account_id": "ACCOUNT", "revision": 1,
        "strategies": {
            "ALPHA": {"actual_positions": alpha},
            "BETA": {"actual_positions": beta or {}},
        },
    }


def _portfolio(alpha, beta=None) -> dict:
    positions = {}
    for book in (alpha, beta or {}):
        for instrument, quantity in book.items():
            positions[instrument] = positions.get(instrument, 0) + quantity
    return {
        "account": {"account_id": "ACCOUNT"},
        "active_orders": [],
        "positions": [
            {"instrument": instrument, "direction": "LONG" if quantity > 0 else "SHORT",
             "volume": abs(quantity)}
            for instrument, quantity in positions.items() if quantity
        ],
    }


if __name__ == "__main__":
    unittest.main()
