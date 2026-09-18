import json
from decimal import Decimal
from pathlib import Path
import unittest

from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
    normalize_portfolio_snapshot,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sim_trading_snapshot.json"


class SimTradingNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.raw = {
            "success": True,
            "data": {
                **event["payload"],
                "business_version": event["business_version"],
            },
        }

    def test_normalizes_decimal_values_without_float_rounding(self) -> None:
        snapshot = normalize_portfolio_snapshot(self.raw, "ETF-OPTION-1")

        self.assertEqual(snapshot.account.available_cash, Decimal("800000.05"))
        self.assertEqual(snapshot.positions[0].volume, Decimal("3"))
        self.assertEqual(snapshot.positions[0].signed_volume, Decimal("3"))
        self.assertEqual(snapshot.active_orders[0].remaining_volume, Decimal("3"))
        self.assertEqual(snapshot.business_version, "12345")

    def test_required_market_instruments_include_positions_and_active_orders(self) -> None:
        snapshot = normalize_portfolio_snapshot(self.raw, "ETF-OPTION-1")

        self.assertEqual(
            snapshot.instruments,
            ("10008888.XSHE", "10009999.XSHE"),
        )

    def test_rejects_an_account_mismatch(self) -> None:
        with self.assertRaisesRegex(SimTradingError, "account mismatch"):
            normalize_portfolio_snapshot(self.raw, "OTHER")

    def test_accepts_websocket_snapshot_envelope(self) -> None:
        event = json.loads(FIXTURE.read_text(encoding="utf-8"))

        snapshot = normalize_portfolio_snapshot(event, "ETF-OPTION-1")

        self.assertEqual(snapshot.account.account_id, "ETF-OPTION-1")
        self.assertEqual(snapshot.business_version, "12345")

    def test_accepts_trading_snapshot_position_with_pnl(self) -> None:
        snapshot = normalize_portfolio_snapshot(self.raw, "ETF-OPTION-1")

        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].instrument, "10009999.XSHE")

    def test_derives_total_volume_from_today_and_yesterday(self) -> None:
        snapshot = normalize_portfolio_snapshot(self.raw, "ETF-OPTION-1")

        self.assertEqual(snapshot.positions[0].today_volume, Decimal("1"))
        self.assertEqual(snapshot.positions[0].yesterday_volume, Decimal("2"))
        self.assertEqual(snapshot.positions[0].volume, Decimal("3"))


class SimTradingSourceTests(unittest.TestCase):
    def test_source_uses_only_read_endpoint_for_portfolio(self) -> None:
        calls = []
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            return fixture

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=request,
        )

        snapshot = source.load("ETF-OPTION-1")

        self.assertEqual(snapshot.account.account_id, "ETF-OPTION-1")
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(
            calls[0][1],
            "http://simulator.test/api/accounts/ETF-OPTION-1/trading-snapshot",
        )
        self.assertEqual(calls[0][3], None)

    def test_requires_authentication_before_get(self) -> None:
        source = SimTradingPortfolioSource(
            "http://simulator.test", request_json=lambda *args: {}
        )

        with self.assertRaisesRegex(SimTradingError, "not authenticated"):
            source.load("ETF-OPTION-1")


if __name__ == "__main__":
    unittest.main()
