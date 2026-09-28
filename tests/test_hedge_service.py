from datetime import datetime, timezone
import unittest

from hedge_engine.config import HedgeConfig
from hedge_service import HedgeInstrument, HedgeRequest, ReferenceHedgeEngine


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
