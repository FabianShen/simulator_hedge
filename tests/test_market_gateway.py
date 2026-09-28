"""Exercise the market process's pricing-only and external-hedge boundaries."""

import contextlib
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hedge_engine import ConfirmedFill, apply_confirmed_fills, empty_ledger
from hedge_engine.config import HedgeConfig
from hedge_service import ReferenceHedgeEngine
from hedge_service.grpc_server import create_server
from sim_hedge import __main__ as gateway
from sim_hedge.adapters.grpc_hedging import HedgeServiceError
from sim_hedge.domain.contract import OptionContract, OptionType
from sim_hedge.domain.quote import MarketQuote
from sim_hedge.domain.pricing import OptionValuation, PricingBatch, PricingServiceHealth, SabrFit
from sim_hedge.state.ledger import ledger_to_payload


NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class _OneShotSource:
    def __init__(self) -> None:
        self.channels = ["tick_ETF", "tick_ALPHA", "tick_CALL", "tick_PUT"]
        self.health = SimpleNamespace(state="running", data_unsafe=False)
        self.ran = False

    def run(self, on_quote) -> None:
        self.ran = True
        now = datetime.now(timezone.utc)
        on_quote(MarketQuote("ETF", now, now, now.date(), 3.3, 3.3, 3.31))

    def stop(self) -> None:
        self.health.state = "stopped"


class _OneShotPricingWorker:
    def __init__(self, client, make_request, *, on_priced, **kwargs) -> None:
        self._on_priced = on_priced
        self.health = "test worker stopped"

    def start(self) -> None:
        pass

    def request_update(self) -> None:
        self._on_priced(_request(), _pricing())

    def stop(self) -> None:
        pass

    def latest(self):
        return None


class MarketGatewayHedgeBoundaryTests(unittest.TestCase):
    def test_pricing_only_needs_no_hedge_service(self) -> None:
        with patch("sim_hedge.__main__.GrpcHedgeClient", side_effect=AssertionError("hedge service used")):
            risk, source = self._run_gateway()
        self.assertIsNone(risk)
        self.assertTrue(source.ran)

    def test_ledger_requires_hedge_target_before_live_start(self) -> None:
        with patch.dict(os.environ, {"PRICING_TARGET": "", "HEDGE_TARGET": ""}), \
             patch.object(sys, "argv", ["sim_hedge", "--pricing-target", "local", "--strategy-ledger", "ledger.json"]), \
             patch("sim_hedge.__main__.load_env_file"), \
             patch("sim_hedge.__main__.load_option_chain", side_effect=AssertionError("live startup")):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    gateway.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("--strategy-ledger requires --hedge-target", errors.getvalue())

    def test_option_chain_diagnostic_does_not_require_hedge_service(self) -> None:
        with patch.dict(os.environ, {"PRICING_TARGET": "", "HEDGE_TARGET": ""}), \
             patch.object(sys, "argv", ["sim_hedge", "--check-options", "ETF", "--strategy-ledger", "ledger.json"]), \
             patch("sim_hedge.__main__.load_env_file"), \
             patch("sim_hedge.__main__.load_option_chain", return_value=_contracts()), \
             patch("sim_hedge.__main__.YmmLiveDataSource", side_effect=AssertionError("live startup")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            gateway.main()
        self.assertIn("underlying: ETF", output.getvalue())

    def test_successful_service_proposal_is_published_without_recalculation(self) -> None:
        server, port = create_server(
            "127.0.0.1:0",
            engine=ReferenceHedgeEngine(config=_dg_config()),
            clock=lambda: NOW,
        )
        server.start()
        try:
            risk, source = self._run_gateway(ledger=True, hedge_target=f"127.0.0.1:{port}")
        finally:
            server.stop(grace=None).wait()
        self.assertTrue(source.ran)
        self.assertEqual(risk["status"], "READY")
        self.assertEqual(risk["source_pricing_request_id"], "pricing-1")
        self.assertEqual(risk["source_market_as_of"], NOW.isoformat())
        self.assertEqual(risk["base_strategy_ledger_revision"], 1)
        self.assertEqual(risk["incremental_trades"], {"A": 1, "B": 1, "C": 1})
        self.assertEqual(risk["target_beta_positions"], {"A": 1, "B": 1, "C": 1})
        self.assertEqual(
            {leg["instrument"] for leg in risk["execution_diagnostics"]["legs"]},
            {"A", "B", "C"},
        )
        self.assertIn(
            "estimated_short_margin_before", risk["execution_diagnostics"]
        )
        self.assertFalse(risk["orders_generated"])
        self.assertIn("pricing_calculated_at", risk)
        self.assertIn("published_at", risk)

    def test_no_trade_service_proposal_is_published_unchanged(self) -> None:
        server, port = create_server(
            "127.0.0.1:0",
            engine=ReferenceHedgeEngine(),
            clock=lambda: NOW,
        )
        server.start()
        try:
            risk, _ = self._run_gateway(ledger=True, hedge_target=f"127.0.0.1:{port}")
        finally:
            server.stop(grace=None).wait()
        self.assertEqual(risk["status"], "READY")
        self.assertEqual(risk["incremental_trades"], {})
        self.assertEqual(risk["target_beta_positions"], risk["confirmed_beta_positions"])
        self.assertEqual(risk["hedge_pair"], [])
        self.assertFalse(risk["orders_generated"])

    def test_hedge_rpc_failure_publishes_not_ready_without_fallback(self) -> None:
        client = Mock()
        client.health.return_value = {
            "engine_name": "unavailable", "engine_version": "1", "protocol_version": "hedging.v1"
        }
        client.propose.side_effect = HedgeServiceError("hedge request failed [UNAVAILABLE]")
        with patch("sim_hedge.__main__.GrpcHedgeClient", return_value=client):
            risk, source = self._run_gateway(ledger=True, hedge_target="local")
        self.assertTrue(source.ran)
        self.assertEqual(risk["status"], "NOT_READY")
        self.assertEqual(risk["source_pricing_request_id"], "pricing-1")
        self.assertFalse(risk["orders_generated"])
        self.assertIn("UNAVAILABLE", risk["error"])
        self.assertNotIn("incremental_trades", risk)
        client.propose.assert_called_once()

    def _run_gateway(self, *, ledger: bool = False, hedge_target: str = ""):
        with tempfile.TemporaryDirectory() as directory:
            risk_path = Path(directory) / "risk.json"
            ledger_path = Path(directory) / "ledger.json"
            if ledger:
                ledger_path.write_text(json.dumps(ledger_to_payload(_ledger())), encoding="utf-8")
            source = _OneShotSource()
            args = [
                "sim_hedge", "ETF", "--pricing-target", "local", "--max-quotes", "1",
                "--snapshot", "", "--alpha-market-output", "", "--pricing-output", "",
                "--risk-output", str(risk_path),
            ]
            if ledger:
                args.extend(["--strategy-ledger", str(ledger_path), "--hedge-target", hedge_target])
            pricing_client = Mock()
            pricing_client.health.return_value = PricingServiceHealth("pricing", "1", "pricing.v1")
            with patch.dict(os.environ, {"LIVE_TOKEN": "test", "PRICING_TARGET": "", "HEDGE_TARGET": ""}), \
                 patch.object(sys, "argv", args), \
                 patch("sim_hedge.__main__.load_env_file"), \
                 patch("sim_hedge.__main__.load_option_chain", return_value=_contracts()), \
                 patch("sim_hedge.__main__.YmmLiveDataSource", return_value=source), \
                 patch("sim_hedge.__main__.GrpcPricingClient", return_value=pricing_client), \
                 patch("sim_hedge.__main__.ContinuousPricingWorker", _OneShotPricingWorker), \
                 patch("sim_hedge.__main__.MarketMonitor"), \
                 contextlib.redirect_stdout(io.StringIO()):
                gateway.main()
            return (
                json.loads(risk_path.read_text(encoding="utf-8")) if risk_path.exists() else None,
                source,
            )


def _contracts() -> list[OptionContract]:
    return [
        OptionContract("ALPHA", "ETF", OptionType.CALL, 3.4, date(2026, 9, 23), 1, 0.0001),
        OptionContract("CALL", "ETF", OptionType.CALL, 3.3, date(2026, 9, 23), 1, 0.0001),
        OptionContract("PUT", "ETF", OptionType.PUT, 3.3, date(2026, 9, 23), 1, 0.0001),
    ]


def _ledger():
    fill = ConfirmedFill("T1", "O1", "A1", "ALPHA", "ALPHA", -1, Decimal("0.1"), NOW)
    return apply_confirmed_fills(empty_ledger("A1"), (fill,))


def _request() -> dict:
    def option(instrument, option_type, strike):
        depth = 0 if instrument in {"CALL", "PUT"} else 1
        result = {"instrument": instrument, "optionType": option_type, "strike": strike,
                  "contractMultiplier": 1, "bid": 0.1, "ask": 0.1,
                  "bidSize": depth, "askSize": depth}
        if instrument != "ALPHA":
            result["marketPrice"] = 0.1
        return result
    return {
        "requestId": "pricing-1", "asOf": NOW.isoformat(),
        "underlying": {"instrument": "ETF", "spot": 3.3},
        "options": [option("ALPHA", "OPTION_TYPE_CALL", 3.4),
                    option("CALL", "OPTION_TYPE_CALL", 3.3),
                    option("PUT", "OPTION_TYPE_PUT", 3.3),
                    option("A", "OPTION_TYPE_CALL", 3.4),
                    option("B", "OPTION_TYPE_CALL", 3.5),
                    option("C", "OPTION_TYPE_CALL", 3.6)],
    }


def _pricing() -> PricingBatch:
    def valuation(instrument, delta, gamma):
        return OptionValuation(instrument, "OK", None, 0.1, 0.2, 0.2, 0.0,
                               delta, gamma, -0.1, 0.2, 0.1)
    return PricingBatch(
        "pricing-1", NOW, "pricing", "1", "SABR_BLACK_76",
        SabrFit(3.3, 0.3, 0.5, 0.8, -0.2, 0.001, 3),
        (
            valuation("ALPHA", 1, 2),
            valuation("CALL", 0, 0),
            valuation("PUT", 0, 0),
            valuation("A", 0.4, 0.8),
            valuation("B", 0.3, 1.0),
            valuation("C", 0.3, 0.2),
        ),
    )


def _dg_config() -> HedgeConfig:
    return HedgeConfig(
        option_fee=0.0,
        delta_entry_risk_band=0.03,
        delta_target_risk_band=0.0001,
        gamma_entry_risk_band=0.001,
        gamma_target_risk_band=0.00001,
    )


if __name__ == "__main__":
    unittest.main()
