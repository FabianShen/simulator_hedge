import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hedge_engine import HedgeConfig, validate_hedge_proposal
from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine

from pricing_engine import OptionPricingResult, SabrPricingEngine
from pricing_engine.__main__ import load_request
from sim_hedge.hedge_client.plan import _usable_instrument_greeks, build_offline_hedge_decision, main


AS_OF = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
DELTA_ENTRY = 1_000.0
DELTA_TARGET = 100.0
GAMMA_ENTRY = 100_000.0
GAMMA_TARGET = 50_000.0
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
    def test_cli_maps_position_limit_and_zero_to_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pricing_path = root / "pricing.json"
            portfolio_path = root / "portfolio.json"
            ledger_path = root / "ledger.json"
            for path in (pricing_path, portfolio_path, ledger_path):
                path.write_text("{}", encoding="utf-8")

            for arguments, expected, penalty in (
                ([], 300, 20),
                (["--position-limit", "2000"], 2_000, 20),
                (["--position-limit", "0"], None, 20),
                (["--depth-excess-penalty", "0"], 300, 0),
                (["--depth-excess-penalty", "12.5"], 300, 12.5),
            ):
                with self.subTest(arguments=arguments):
                    received = {}

                    def capture_config(*args, **kwargs):
                        received["config"] = kwargs["config"]
                        raise RuntimeError("stop after inspecting CLI config")

                    with patch.object(sys, "argv", [
                        "hedge_plan", str(pricing_path), str(portfolio_path),
                        str(ledger_path), *arguments,
                    ]), patch(
                        "sim_hedge.hedge_client.plan.build_offline_hedge_decision",
                        side_effect=capture_config,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "stop after"):
                            main()

                    self.assertEqual(received["config"].v2_position_limit, expected)
                    self.assertEqual(received["config"].v2_depth_excess_penalty, penalty)

    def test_cli_rejects_invalid_depth_excess_penalty(self) -> None:
        for value in ("-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(sys, "argv", [
                    "hedge_plan", "pricing.json", "portfolio.json", "ledger.json",
                    f"--depth-excess-penalty={value}",
                ]):
                    with self.assertRaises(SystemExit) as raised:
                        main()
            self.assertEqual(raised.exception.code, 2)

    def test_cli_rejects_negative_or_fractional_position_limits(self) -> None:
        for value in ("-1", "1.5"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(sys, "argv", [
                    "hedge_plan", "pricing.json", "portfolio.json", "ledger.json",
                    "--position-limit", value,
                ]):
                    with self.assertRaises(SystemExit) as raised:
                        main()
            self.assertEqual(raised.exception.code, 2)

    def test_offline_cli_default_position_limit_preserves_legacy_inventory(self) -> None:
        pricing = _recorded_pricing()
        beta = {"C-3.45": 1_315}
        priced_results = tuple(
            OptionPricingResult(
                instrument=option["instrument"],
                status="OK",
                delta=(
                    1.0 if option["instrument"] == "C-3.45"
                    else 0.0
                ),
                gamma=0.0,
                theta_per_year=0.0,
                vega_per_absolute_volatility=0.0,
            )
            for option in pricing["options"]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pricing_path = _write_pricing(root, pricing)
            portfolio_path = root / "portfolio.json"
            ledger_path = root / "ledger.json"
            output_path = root / "hedge_plan.json"
            portfolio_path.write_text(
                json.dumps(_portfolio({}, beta)), encoding="utf-8"
            )
            ledger_path.write_text(json.dumps(_ledger({}, beta)), encoding="utf-8")

            output = {}
            with patch.object(
                SabrPricingEngine,
                "price",
                return_value=SimpleNamespace(results=priced_results),
            ):
                for label, extra_args in (
                    ("default", []),
                    ("raised", ["--position-limit", "2000"]),
                ):
                    with patch.object(sys, "argv", [
                        "hedge_plan", str(pricing_path), str(portfolio_path),
                        str(ledger_path), "--output", str(output_path),
                        "--target-delta", "0", "--delta-limit", "11150000",
                        *extra_args,
                    ]), contextlib.redirect_stdout(io.StringIO()):
                        main()
                    output[label] = json.loads(
                        output_path.read_text(encoding="utf-8")
                    )

        self.assertEqual(output["default"]["decision_policy"], "D_G_MILP")
        self.assertEqual(output["default"]["incremental_trades"], {"C-3.45": -200})
        self.assertEqual(output["default"]["target_beta_positions"], {"C-3.45": 1_115})
        self.assertEqual(output["raised"]["decision_policy"], "D_G_MILP")
        self.assertTrue(output["raised"]["incremental_trades"])

    def test_raw_delta_target_offline_cli_replay_keeps_reported_greeks_raw(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": 1}

        def response_with_alpha_delta(delta: float):
            candidate_deltas = {"C-3.45": delta, "C-3.6": 0.05}
            results = tuple(
                OptionPricingResult(
                    instrument=option["instrument"],
                    status="OK",
                    delta=candidate_deltas.get(option["instrument"], 0.0),
                    gamma=0.0,
                    theta_per_year=0.0,
                    vega_per_absolute_volatility=0.0,
                )
                for option in pricing["options"]
            )
            return SimpleNamespace(results=results)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pricing_path = _write_pricing(root, pricing)
            portfolio_path = root / "portfolio.json"
            ledger_path = root / "ledger.json"
            output_path = root / "hedge_plan.json"
            results = {}

            with patch.object(
                SabrPricingEngine,
                "price",
                side_effect=(response_with_alpha_delta(0.5), response_with_alpha_delta(0.65)),
            ):
                for label, delta in (("center", 5_000.0), ("outside", 6_500.0)):
                    portfolio_path.write_text(
                        json.dumps(_portfolio(alpha)), encoding="utf-8"
                    )
                    ledger_path.write_text(
                        json.dumps(_ledger(alpha)), encoding="utf-8"
                    )
                    stdout = io.StringIO()
                    with patch.object(sys, "argv", [
                        "hedge_plan", str(pricing_path), str(portfolio_path),
                        str(ledger_path), "--output", str(output_path),
                        "--target-delta", "5000", "--delta-limit", "1000",
                        "--target-gamma", "0", "--gamma-limit", "3000",
                    ]), contextlib.redirect_stdout(stdout):
                        main()
                    results[label] = (
                        json.loads(output_path.read_text(encoding="utf-8")),
                        stdout.getvalue(),
                    )

        centered, centered_stdout = results["center"]
        self.assertIn("before delta=5000.000000", centered_stdout)
        self.assertEqual(centered["risk"]["before_hedge"]["delta"], 5_000.0)
        self.assertEqual(centered["decision_policy"], "MSH")
        self.assertEqual(centered["incremental_trades"], {})

        outside, outside_stdout = results["outside"]
        self.assertIn("before delta=6500.000000", outside_stdout)
        self.assertEqual(outside["risk"]["before_hedge"]["delta"], 6_500.0)
        self.assertEqual(outside["decision_policy"], "D_G_MILP")
        self.assertEqual(outside["incremental_trades"], {"C-3.6": -1})
        self.assertEqual(outside["risk_at_target_beta"]["delta"], 6_000.0)
        self.assertLessEqual(
            abs(outside["risk_at_target_beta"]["delta"] - 5_000.0), 1_000.0
        )

    def test_breached_risk_uses_reference_engine_pair_and_trade(self) -> None:
        pricing = _recorded_pricing()
        alpha = {"C-3.45": -1000}
        with tempfile.TemporaryDirectory() as directory:
            path = _write_pricing(Path(directory), pricing)
            result, context, exclusions = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha), _ledger(alpha),
                config=_replay_config(),
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
                    contract_multiplier=item["contractMultiplier"],
                    delta=results[item["instrument"]].delta,
                    gamma=results[item["instrument"]].gamma,
                    theta=results[item["instrument"]].theta_per_year,
                    vega=results[item["instrument"]].vega_per_absolute_volatility,
                    bid=item.get("bid"),
                    ask=item.get("ask"),
                    bid_size=item.get("bidSize"),
                    ask_size=item.get("askSize"),
                )
                for item in pricing["options"]
            ),
            confirmed_alpha_positions=alpha,
            confirmed_beta_positions={},
            hedge_universe=context.hedge_universe,
        )
        expected = ReferenceHedgeEngine(config=_replay_config()).propose(
            expected_request, created_at=AS_OF
        )
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
                config=_replay_config(minimum_target_gross_reduction=100_000),
            )
            beta = first.proposal["target_beta_positions"]
            inside, _, _ = build_offline_hedge_decision(
                path, pricing, _portfolio(alpha, beta), _ledger(alpha, beta),
                config=_replay_config(minimum_target_gross_reduction=100_000),
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
                config=_replay_config(),
            )
        priced = {item.instrument: item for item in response.results}[held_beta]

        self.assertEqual(exclusions, {})
        self.assertIn(held_beta, context.instrument_greeks)
        self.assertNotIn(held_beta, context.hedge_universe)
        self.assertAlmostEqual(
            result.confirmed_beta_risk.delta, 7 * 10_000 * priced.delta
        )
        self.assertAlmostEqual(
            result.confirmed_beta_risk.gamma, 7 * 10_000 * priced.gamma
        )
        self.assertNotIn(held_beta, result.proposal["incremental_trades"])

    def test_unquoted_short_alpha_fails_closed_without_live_margin_quote(self) -> None:
        pricing = _recorded_pricing()
        held_alpha = "C-3.45"
        held_option = next(
            item for item in pricing["options"]
            if item["instrument"] == held_alpha
        )
        held_option.pop("marketPrice")
        held_option.pop("observedAt")
        for field in ("bid", "ask", "bidSize", "askSize"):
            held_option.pop(field, None)

        with self.assertRaisesRegex(ValueError, "live two-sided quote.*C-3.45"):
            _decision(pricing)

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
                    config=_replay_config(),
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
                "--delta-entry-risk-band", str(DELTA_ENTRY),
                "--delta-target-risk-band", str(DELTA_TARGET),
                "--gamma-entry-risk-band", str(GAMMA_ENTRY),
                "--gamma-target-risk-band", str(GAMMA_TARGET),
                "--target-delta", "5000", "--delta-limit", "1000",
                "--target-gamma", "0", "--gamma-limit", "3000",
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
        self.assertIn("gamma_improvement", output)
        execution = output["execution_diagnostics"]
        self.assertIn("estimated_transaction_cost", execution)
        self.assertIn("estimated_short_margin_before", execution)
        self.assertIn("estimated_short_margin_after", execution)
        self.assertIn("short_margin_limit", execution)
        self.assertEqual(
            {leg["instrument"] for leg in execution["legs"]},
            set(output["incremental_trades"]),
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
            config=_replay_config(),
        )


def _replay_config(**overrides):
    values = dict(
        option_multiplier=10_000,
        option_fee=0.0,
        delta_entry_risk_band=DELTA_ENTRY,
        delta_target_risk_band=DELTA_TARGET,
        gamma_entry_risk_band=GAMMA_ENTRY,
        gamma_target_risk_band=GAMMA_TARGET,
    )
    values.update(overrides)
    return HedgeConfig(**values)


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
                "contractMultiplier": 10_000,
                "marketPrice": item["market_price"],
                "bid": item["market_price"],
                "ask": item["market_price"],
                "bidSize": 1000,
                "askSize": 1000,
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
             "volume": str(abs(quantity))}
            for instrument, quantity in positions.items() if quantity
        ],
    }


if __name__ == "__main__":
    unittest.main()
