import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from hedge_engine import ConfirmedFill, StrategyLedger
from sim_hedge.domain import OptionContract, OptionType
from sim_hedge.live_risk import build_live_risk_snapshot, write_live_risk_snapshot
from sim_hedge.pricing_types import OptionValuation, PricingBatch, SabrFit
from sim_hedge.strategy_universe import StrategyUniverse


NOW = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc)


class LiveRiskTests(unittest.TestCase):
    def test_held_alpha_outside_tradable_universe_contributes_risk(self) -> None:
        tradable = _universe()
        tradable = StrategyUniverse(
            tradable.underlying, tradable.maturity, tradable.center_strike,
            tradable.strikes,
            tuple(item for item in tradable.contracts if item.instrument != "ALPHA"),
        )
        snapshot = build_live_risk_snapshot(
            _pricing(), tradable, _ledger(), spot=3.3,
            valuation_contracts=(_contract("ALPHA", OptionType.CALL, 3.4),),
        )
        self.assertEqual(snapshot["status"], "READY")
        self.assertEqual(snapshot["risk"]["alpha"]["delta"], -2)
        self.assertEqual(snapshot["hedge_pair"], ["CALL", "PUT"])

    def test_builds_risk_and_desired_beta_without_orders(self) -> None:
        snapshot = build_live_risk_snapshot(
            _pricing(), _universe(), _ledger(), spot=3.3, published_at=NOW
        )

        self.assertEqual(snapshot["status"], "READY")
        self.assertEqual(snapshot["protocol_version"], "sim-hedge/hedge-proposal/v1")
        self.assertTrue(snapshot["proposal_id"].startswith("hedge-"))
        self.assertEqual(snapshot["base_strategy_ledger_revision"], 1)
        self.assertEqual(snapshot["hedge_pair"], ["CALL", "PUT"])
        self.assertEqual(
            snapshot["incremental_trades"], {"CALL": 3, "PUT": 1}
        )
        self.assertEqual(snapshot["target_beta_positions"], {"CALL": 3, "PUT": 1})
        self.assertEqual(snapshot["risk"]["portfolio"]["delta"], -2.0)
        self.assertEqual(snapshot["risk"]["portfolio"]["gamma"], -4.0)
        self.assertFalse(snapshot["orders_generated"])

    def test_rejects_missing_greeks_for_a_held_position(self) -> None:
        pricing = _pricing(results=(_valuation("CALL", 1, 1), _valuation("PUT", -1, 1)))

        with self.assertRaisesRegex(ValueError, "valid pricing Greeks missing for ALPHA"):
            build_live_risk_snapshot(pricing, _universe(), _ledger(), spot=3.3)

    def test_atomically_writes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_risk.json"
            write_live_risk_snapshot(path, {"status": "READY"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "READY"})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


def _ledger() -> StrategyLedger:
    fill = ConfirmedFill(
        trade_id="T1",
        order_id="O1",
        account_id="ACCOUNT",
        strategy="ALPHA",
        instrument="ALPHA",
        quantity=-1,
        price=Decimal("0.1"),
        executed_at=NOW,
    )
    return StrategyLedger(
        account_id="ACCOUNT",
        revision=1,
        alpha_positions={"ALPHA": -1},
        beta_positions={},
        applied_trades={"T1": fill},
    )


def _universe() -> StrategyUniverse:
    contracts = (
        _contract("ALPHA", OptionType.CALL, 3.4),
        _contract("CALL", OptionType.CALL, 3.3),
        _contract("PUT", OptionType.PUT, 3.3),
    )
    return StrategyUniverse(
        underlying="ETF",
        maturity=date(2026, 9, 23),
        center_strike=3.3,
        strikes=(3.3, 3.4),
        contracts=contracts,
    )


def _contract(instrument: str, option_type: OptionType, strike: float) -> OptionContract:
    return OptionContract(
        instrument=instrument,
        underlying="ETF",
        option_type=option_type,
        strike=strike,
        maturity=date(2026, 9, 23),
        contract_multiplier=1,
        price_tick=0.0001,
    )


def _pricing(results=None) -> PricingBatch:
    return PricingBatch(
        request_id="pricing-1",
        calculated_at=NOW,
        engine_name="test",
        engine_version="1",
        model="SABR_BLACK_76",
        calibration=SabrFit(3.3, 0.3, 0.5, 0.8, -0.2, 0.001, 3),
        results=results
        or (
            _valuation("ALPHA", 2, 4),
            _valuation("CALL", 1, 1),
            _valuation("PUT", -1, 1),
        ),
    )


def _valuation(instrument: str, delta: float, gamma: float) -> OptionValuation:
    return OptionValuation(
        instrument=instrument,
        status="OK",
        error=None,
        theoretical_price=0.1,
        market_implied_volatility=0.2,
        model_implied_volatility=0.2,
        implied_volatility_error=0.0,
        delta=delta,
        gamma=gamma,
        theta_per_year=-0.1,
        vega_per_absolute_volatility=0.2,
        rho_per_absolute_rate=0.1,
    )


if __name__ == "__main__":
    unittest.main()
