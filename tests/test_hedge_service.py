from dataclasses import replace
from datetime import datetime, timezone
import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch

from hedge_engine.config import HedgeConfig
from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine
from hedge_service.grpc_server import main as hedge_server_main
from hedge_service.protobuf_codec import response_to_proto


NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


def make_request(*, alpha=None, beta=None, instruments=None, universe=None):
    if instruments is None:
        instruments = (
            _instrument("ALPHA", "CALL", 105, 2.0, 2.0),
            _instrument("CALL", "CALL", 100, 1.0, 1.0),
            _instrument("PUT", "PUT", 100, -1.0, 1.0),
            _instrument("CALL_W", "CALL", 110, 0.5, 1.0),
            _instrument("PUT_W", "PUT", 90, -0.5, 1.0),
        )
    return HedgeRequest(
        request_id="hedge-test",
        source_pricing_request_id="pricing-test",
        market_as_of=NOW,
        account_id="ACCOUNT",
        base_ledger_revision=1,
        spot=100.0,
        instruments=tuple(instruments),
        confirmed_alpha_positions={"ALPHA": -1} if alpha is None else alpha,
        confirmed_beta_positions={} if beta is None else beta,
        hedge_universe=("CALL", "PUT", "CALL_W", "PUT_W") if universe is None else tuple(universe),
    )


def _instrument(code, option_type, strike, delta, gamma, *, bid=1.0, ask=1.0,
                bid_size=100, ask_size=100, multiplier=1):
    return HedgeInstrument(
        instrument=code,
        option_type=option_type,
        strike=strike,
        contract_multiplier=multiplier,
        delta=delta,
        gamma=gamma,
        theta=0.0,
        vega=0.0,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )


def dg_config(**overrides):
    values = dict(
        option_fee=0.0,
        v2_trade_limit=10,
        v2_position_limit=20,
        delta_entry_risk_band=1.5,
        delta_target_risk_band=0.2,
        gamma_entry_risk_band=0.75,
        gamma_target_risk_band=0.1,
    )
    values.update(overrides)
    return HedgeConfig(**values)


class ReferenceHedgeServiceTests(unittest.TestCase):
    def test_server_cli_depth_excess_penalty_defaults_and_overrides(self) -> None:
        for arguments, expected in (([], 20), (["--depth-excess-penalty", "0"], 0),
                                    (["--depth-excess-penalty", "12.5"], 12.5)):
            with self.subTest(arguments=arguments):
                server = Mock()
                server.wait_for_termination.side_effect = KeyboardInterrupt
                with patch.object(sys, "argv", ["grpc_server", *arguments]), patch(
                    "hedge_service.grpc_server.create_server", return_value=(server, 50052),
                ) as create, contextlib.redirect_stdout(io.StringIO()):
                    hedge_server_main()
                self.assertEqual(create.call_args.kwargs["engine"].config.v2_depth_excess_penalty, expected)

    def test_server_cli_rejects_invalid_depth_excess_penalty(self) -> None:
        for value in ("-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(sys, "argv", ["grpc_server", f"--depth-excess-penalty={value}"]):
                    with self.assertRaises(SystemExit) as raised:
                        hedge_server_main()
                self.assertEqual(raised.exception.code, 2)

    def test_shallow_depth_double_breach_produces_trades_and_excess_diagnostics(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1},
            instruments=(
                _instrument("ALPHA", "CALL", 105, -51, 277, multiplier=10_000),
                _instrument("D", "CALL", 110, 0.5, 0, bid_size=1, ask_size=1, multiplier=10_000),
                _instrument("G", "CALL", 120, 0, 2, bid_size=5, ask_size=5, multiplier=10_000),
            ),
            universe=("D", "G"),
        )
        for penalty in (20, 0):
            with self.subTest(penalty=penalty):
                config = HedgeConfig(
                    option_fee=0, target_delta=-50_000, delta_limit=10_000,
                    target_gamma=-80_000, gamma_limit=10_000,
                    v2_depth_excess_penalty=penalty,
                )
                result = ReferenceHedgeEngine(config=config).propose(request, created_at=NOW)
                trades = result.proposal["incremental_trades"]
                self.assertEqual(result.decision_policy, "D_G_MILP")
                self.assertGreater(trades["D"], 1)
                self.assertLess(trades["G"], -5)
                self.assertGreater(sum(abs(q) for q in trades.values()), 100)
                self.assertTrue(all(abs(q) <= 200 for q in trades.values()))
                self.assertTrue(all(abs(q) <= 300 for q in result.proposal["target_beta_positions"].values()))
                self.assertLessEqual(abs(result.risk_at_target_beta.delta + 50_000), 10_000)
                self.assertLessEqual(abs(result.risk_at_target_beta.gamma + 80_000), 10_000)
                diagnostics = result.execution_diagnostics
                excess = max(trades["D"] - 1, 0) + max(-trades["G"] - 5, 0)
                self.assertEqual(diagnostics.estimated_transaction_cost, penalty * excess)
                self.assertEqual(sum(leg.estimated_transaction_cost for leg in diagnostics.legs), penalty * excess)
                self.assertTrue(all(leg.displayed_depth_limit is None for leg in diagnostics.legs))
                self.assertLessEqual(diagnostics.estimated_short_margin_after, diagnostics.short_margin_limit)
                wire = response_to_proto(request.request_id, result)
                self.assertEqual(wire.execution_diagnostics.estimated_transaction_cost, penalty * excess)
                self.assertTrue(all(not leg.HasField("displayed_depth_limit")
                                    for leg in wire.execution_diagnostics.legs))

    def test_gamma_fallback_high_penalty_stays_within_displayed_depth(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1},
            instruments=(
                _instrument("ALPHA", "CALL", 105, 0, 1000),
                _instrument("G", "CALL", 110, 0, 100, bid_size=2, ask_size=0),
            ),
            universe=("G",),
        )
        # With a cap of five the target band is infeasible; exercise fallback.
        for penalty, quantity in ((0, -5), (20, -5), (1e6, -2)):
            with self.subTest(penalty=penalty):
                result = ReferenceHedgeEngine(config=dg_config(
                    v2_trade_limit=5, v2_depth_excess_penalty=penalty,
                )).propose(request, created_at=NOW)
                self.assertEqual(result.proposal["incremental_trades"], {"G": quantity})
                self.assertEqual(result.execution_diagnostics.estimated_transaction_cost,
                                 max(-quantity - 2, 0) * penalty)

    def test_zero_depth_is_soft_and_missing_depth_uses_trade_cap(self) -> None:
        for size, cost in ((0, 200), (None, 0)):
            with self.subTest(size=size):
                request = make_request(
                    alpha={"ALPHA": 1},
                    instruments=(
                        _instrument("ALPHA", "CALL", 105, 0, 1000),
                        _instrument("G", "CALL", 110, 0, 100, bid_size=size, ask_size=size),
                    ),
                    universe=("G",),
                )
                result = ReferenceHedgeEngine(config=dg_config()).propose(request, created_at=NOW)
                self.assertEqual(result.proposal["incremental_trades"], {"G": -10})
                self.assertEqual(result.execution_diagnostics.estimated_transaction_cost, cost)

    def test_synthetic_excess_is_charged_once_in_diagnostics(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1},
            instruments=(
                _instrument("ALPHA", "CALL", 105, -1000, 0),
                _instrument("CALL", "CALL", 100, 100, 0, bid_size=0, ask_size=1),
                _instrument("PUT", "PUT", 100, -100, 0, bid_size=2, ask_size=0),
            ),
            universe=("CALL", "PUT"),
        )
        result = ReferenceHedgeEngine(config=dg_config()).propose(request, created_at=NOW)
        self.assertEqual(result.proposal["incremental_trades"], {"CALL": 5, "PUT": -5})
        self.assertEqual(result.execution_diagnostics.estimated_transaction_cost, 4 * 20)
        self.assertEqual(tuple(leg.estimated_transaction_cost for leg in result.execution_diagnostics.legs), (40, 40))

    def test_breached_delta_and_gamma_route_to_multileg_dg(self) -> None:
        instruments = (
            _instrument("ALPHA", "CALL", 105, 1.0, 2.0),
            _instrument("CALL", "CALL", 100, 0.0, 0.0, bid_size=0, ask_size=0),
            _instrument("PUT", "PUT", 100, 0.0, 0.0, bid_size=0, ask_size=0),
            _instrument("A", "CALL", 110, 0.4, 0.8, bid_size=1, ask_size=1),
            _instrument("B", "CALL", 120, 0.3, 1.0, bid_size=1, ask_size=1),
            _instrument("C", "CALL", 130, 0.3, 0.2, bid_size=1, ask_size=1),
        )
        result = ReferenceHedgeEngine(
            config=dg_config(
                delta_entry_risk_band=0.9,
                delta_target_risk_band=0.01,
                gamma_entry_risk_band=0.9,
                gamma_target_risk_band=0.01,
            )
        ).propose(
            make_request(alpha={"ALPHA": -1}, instruments=instruments,
                         universe=("CALL", "PUT", "A", "B", "C")),
            created_at=NOW,
        )
        trades = result.proposal["incremental_trades"]
        self.assertGreaterEqual(len(trades), 3)
        self.assertTrue(set(trades).issubset({"A", "B", "C"}))
        self.assertLess(abs(result.risk_at_target_beta.delta), 1.0)
        self.assertLess(abs(result.risk_at_target_beta.gamma), 1.0)
        self.assertEqual(result.hedge_pair, tuple(sorted(trades)))

    def test_delta_only_breach_still_routes_to_dg(self) -> None:
        config = dg_config(
            gamma_entry_risk_band=2.0,
            gamma_target_risk_band=1.5,
        )
        request = make_request(universe=("CALL", "PUT"))
        result = ReferenceHedgeEngine(config=config).propose(request, created_at=NOW)
        self.assertTrue(result.proposal["incremental_trades"])
        self.assertLess(abs(result.risk_at_target_beta.delta), 1e-9)

    def test_gamma_only_breach_still_routes_to_dg(self) -> None:
        config = dg_config(
            delta_entry_risk_band=100.0,
            delta_target_risk_band=10.0,
            gamma_entry_risk_band=0.75,
            gamma_target_risk_band=0.1,
        )

        result = ReferenceHedgeEngine(config=config).propose(
            make_request(), created_at=NOW
        )

        self.assertTrue(result.proposal["incremental_trades"])
        self.assertLess(abs(result.risk_at_target_beta.gamma), 0.75)

    def test_raw_delta_target_is_honored_and_raw_greeks_remain_reported(self) -> None:
        instruments = (
            _instrument("ALPHA", "CALL", 105, 5_000.0, 0.0),
            _instrument("CANDIDATE", "CALL", 110, 500.0, 0.0),
        )
        engine = ReferenceHedgeEngine(
            config=HedgeConfig(option_fee=0.0, target_delta=5_000.0, delta_limit=1_000.0)
        )
        inside = engine.propose(
            make_request(alpha={"ALPHA": 1}, instruments=instruments,
                         universe=("CANDIDATE",)),
            created_at=NOW,
        )
        outside = engine.propose(
            make_request(
                alpha={"ALPHA": 1},
                instruments=(
                    _instrument("ALPHA", "CALL", 105, 6_500.0, 0.0),
                    instruments[1],
                ),
                universe=("CANDIDATE",),
            ),
            created_at=NOW,
        )

        self.assertEqual(inside.decision_policy, "MSH")
        self.assertEqual(inside.proposal["incremental_trades"], {})
        self.assertEqual(inside.portfolio_risk.delta, 5_000.0)
        self.assertEqual(outside.decision_policy, "D_G_MILP")
        self.assertEqual(outside.proposal["incremental_trades"], {"CANDIDATE": -1})
        self.assertEqual(outside.alpha_risk.delta, 6_500.0)
        self.assertEqual(outside.risk_at_target_beta.delta, 6_000.0)
        self.assertLessEqual(abs(outside.risk_at_target_beta.delta - 5_000.0), 1_000.0)

    def test_raw_gamma_center_is_used_for_trigger_and_improvement(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1},
            instruments=(
                _instrument("ALPHA", "CALL", 105, 0.0, 130.0),
                _instrument("CANDIDATE", "CALL", 110, 0.0, 10.0),
            ),
            universe=("CANDIDATE",),
        )
        engine = ReferenceHedgeEngine(
            config=HedgeConfig(option_fee=0.0, target_gamma=100.0, gamma_limit=20.0)
        )

        result = engine.propose(request, created_at=NOW)

        self.assertEqual(result.decision_policy, "D_G_MILP")
        self.assertEqual(result.proposal["incremental_trades"], {"CANDIDATE": -1})
        self.assertEqual(result.alpha_risk.gamma, 130.0)
        self.assertEqual(result.risk_at_target_beta.gamma, 120.0)
        self.assertAlmostEqual(result.gamma_improvement, 5.0)

    def test_configured_position_limit_allows_hedging_legacy_oversized_beta(self) -> None:
        request = make_request(
            alpha={},
            beta={"CALL": 1_315},
            instruments=(
                _instrument(
                    "CALL", "CALL", 100, 0.5, 0.0,
                    bid_size=10_000, ask_size=10_000,
                ),
            ),
            universe=("CALL",),
        )
        base_config = HedgeConfig(
            option_fee=0.0,
            target_delta=0.0,
            delta_limit=100.0,
            v2_trade_limit=200,
        )

        default = ReferenceHedgeEngine(config=base_config).propose(
            request, created_at=NOW
        )
        enabled = ReferenceHedgeEngine(
            config=replace(base_config, v2_position_limit=2_000)
        ).propose(request, created_at=NOW)

        self.assertEqual(default.decision_policy, "D_G_MILP")
        self.assertEqual(default.proposal["incremental_trades"], {"CALL": -200})
        self.assertEqual(default.proposal["target_beta_positions"], {"CALL": 1_115})
        self.assertEqual(enabled.decision_policy, "D_G_MILP")
        self.assertEqual(enabled.proposal["incremental_trades"], {"CALL": -200})
        self.assertEqual(enabled.proposal["target_beta_positions"], {"CALL": 1_115})

    def test_legacy_inventory_and_zero_depth_delta_breach_select_gamma_neutral_pair(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1}, beta={"LEGACY": 700},
            instruments=(
                _instrument("ALPHA", "CALL", 105, -1000, -200),
                _instrument("LEGACY", "CALL", 130, 0, 0),
                _instrument("CALL", "CALL", 100, 50, 2, ask=3, bid_size=0, ask_size=0),
                _instrument("PUT", "PUT", 100, -50, 2, ask=3, bid_size=0, ask_size=0),
                _instrument("CHEAP", "CALL", 110, 100, -2, bid_size=0, ask_size=0),
            ),
            universe=("CALL", "PUT", "CHEAP"),
        )
        for limit in (300, None):
            with self.subTest(limit=limit):
                result = ReferenceHedgeEngine(config=dg_config(
                    v2_position_limit=limit, delta_entry_risk_band=900,
                    delta_target_risk_band=0.1, gamma_entry_risk_band=200,
                    gamma_target_risk_band=150,
                )).propose(request, created_at=NOW)
                self.assertEqual(result.proposal["incremental_trades"], {"CALL": 10, "PUT": -10})
                self.assertEqual(result.proposal["target_beta_positions"]["LEGACY"], 700)
                self.assertEqual(result.risk_at_target_beta.delta, 0)
                self.assertEqual(result.risk_at_target_beta.gamma, result.portfolio_risk.gamma)
                self.assertEqual(result.execution_diagnostics.estimated_transaction_cost, 220)

    def test_nonworsening_tolerance_uses_same_units_as_the_gate(self) -> None:
        request = make_request(
            alpha={"ALPHA": 1},
            instruments=(
                _instrument("ALPHA", "CALL", 105, -1000, 0),
                _instrument("CALL", "CALL", 100, 50, 2, ask=3),
                _instrument("PUT", "PUT", 100, -50, 2, ask=3),
                _instrument("CHEAP", "CALL", 110, 100, -0.0001),
            ), universe=("CALL", "PUT", "CHEAP"),
        )
        result = ReferenceHedgeEngine(config=dg_config(
            delta_entry_risk_band=900, delta_target_risk_band=0.1,
            gamma_entry_risk_band=2000, gamma_target_risk_band=1000,
        )).propose(request, created_at=NOW)
        self.assertEqual(result.proposal["incremental_trades"], {"CALL": 10, "PUT": -10})
        self.assertEqual(result.risk_at_target_beta.gamma, 0)

    def test_inside_bands_routes_to_stateless_msh_and_respects_depth(self) -> None:
        request = make_request(
            alpha={},
            beta={"CALL": 200},
            instruments=(_instrument(
                "CALL", "CALL", 100, 0, 0, bid_size=1_000, ask_size=1_000
            ),),
            universe=("CALL",),
        )
        result = ReferenceHedgeEngine().propose(request, created_at=NOW)
        self.assertEqual(result.proposal["incremental_trades"], {"CALL": -200})
        self.assertEqual(result.proposal["target_beta_positions"], {})

    def test_msh_does_not_migrate_without_displayed_sell_depth(self) -> None:
        request = make_request(
            alpha={},
            beta={"CALL": 200},
            instruments=(_instrument(
                "CALL", "CALL", 100, 0, 0, bid_size=0, ask_size=1_000
            ),),
            universe=("CALL",),
        )
        result = ReferenceHedgeEngine().propose(request, created_at=NOW)
        self.assertEqual(result.proposal["incremental_trades"], {})

    def test_execution_diagnostics_explain_cost_depth_and_margin(self) -> None:
        request = make_request(
            alpha={},
            beta={"CALL": -200},
            instruments=(_instrument(
                "CALL", "CALL", 100, 0, 0, bid=0.9, ask=1.1,
                bid_size=800, ask_size=1_000,
            ),),
            universe=("CALL",),
        )

        result = ReferenceHedgeEngine().propose(request, created_at=NOW)

        diagnostics = result.execution_diagnostics
        self.assertEqual(result.proposal["incremental_trades"], {"CALL": 200})
        self.assertAlmostEqual(diagnostics.estimated_transaction_cost, 420.0)
        self.assertAlmostEqual(diagnostics.estimated_short_margin_before, 2_600.0)
        self.assertAlmostEqual(diagnostics.estimated_short_margin_after, 0.0)
        self.assertAlmostEqual(diagnostics.short_margin_limit, 70_000_000.0)
        self.assertEqual(len(diagnostics.legs), 1)
        leg = diagnostics.legs[0]
        self.assertEqual(
            (leg.instrument, leg.side, leg.signed_quantity, leg.displayed_size),
            ("CALL", "BUY", 200, 1_000),
        )
        self.assertEqual(leg.displayed_depth_limit, 500)
        self.assertAlmostEqual(leg.estimated_transaction_cost, 420.0)
        self.assertAlmostEqual(leg.short_margin_per_contract, 13.0)

    def test_alpha_beta_overlap_above_thirty_percent_is_rejected(self) -> None:
        request = make_request(alpha={"ALPHA": -10}, beta={"ALPHA": 4})
        with self.assertRaisesRegex(ValueError, "exceeds 30%"):
            ReferenceHedgeEngine(config=dg_config()).propose(request, created_at=NOW)

    def test_mixed_contract_multipliers_are_rejected(self) -> None:
        instruments = (
            _instrument("A", "CALL", 100, 0.5, 0.1, multiplier=1),
            _instrument("B", "PUT", 100, -0.5, 0.1, multiplier=10_000),
        )
        request = make_request(alpha={}, instruments=instruments, universe=("A", "B"))
        with self.assertRaisesRegex(ValueError, "multipliers must be uniform"):
            ReferenceHedgeEngine().propose(request, created_at=NOW)

    def test_short_held_position_requires_live_quote_for_margin(self) -> None:
        unquoted = HedgeInstrument(
            "ALPHA", "CALL", 105, 1, 2, 2, 0, 0,
        )
        request = make_request(alpha={"ALPHA": -1}, instruments=(unquoted,), universe=())
        with self.assertRaisesRegex(ValueError, "live two-sided quote.*ALPHA"):
            ReferenceHedgeEngine(config=dg_config()).propose(request, created_at=NOW)

    def test_proposal_identity_and_gamma_improvement(self) -> None:
        result = ReferenceHedgeEngine().propose(
            make_request(alpha={}, beta={}), created_at=NOW
        )
        self.assertEqual(result.proposal["source_pricing_request_id"], "pricing-test")
        self.assertEqual(result.proposal["base_strategy_ledger_revision"], 1)
        self.assertEqual(result.gamma_improvement, 0.0)
        self.assertFalse(result.proposal["orders_generated"])


if __name__ == "__main__":
    unittest.main()
