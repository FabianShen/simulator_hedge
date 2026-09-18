from datetime import date
import json
from decimal import Decimal
from pathlib import Path
import unittest

from sim_hedge.adapters.sim_trading import (
    SimTradingError,
    SimTradingPortfolioSource,
    SimTradingUnknownOutcomeError,
    normalize_confirmed_trade,
    normalize_portfolio_snapshot,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sim_trading_snapshot.json"


def trade() -> dict:
    return {
        "trade_id": "T-1",
        "order_id": "O-1",
        "account_id": "ETF-OPTION-1",
        "order_book_id": "10009999.XSHE",
        "direction": "SELL",
        "trade_volume": "2",
        "trade_price": "0.1234",
        "trade_time": "2026-09-18T02:00:00Z",
    }


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

    def test_omits_closed_zero_volume_positions_from_snapshot(self) -> None:
        account = self.raw["data"]["accounts"][0]
        closed = json.loads(json.dumps(account["positions"][0]))
        closed["position"]["position_id"] = "CLOSED"
        closed["position"]["today_volume"] = "0"
        closed["position"]["yesterday_volume"] = "0"
        closed["position"]["available_volume"] = "0"
        account["positions"].append(closed)

        snapshot = normalize_portfolio_snapshot(self.raw, "ETF-OPTION-1")

        self.assertEqual([position.position_id for position in snapshot.positions], ["P-1"])


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

    def test_creates_websocket_ticket_with_authenticated_post(self) -> None:
        calls = []

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            return {"ticket": "one-use"}

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=request,
        )

        self.assertEqual(source.websocket_ticket(), "one-use")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "http://simulator.test/api/ws/ticket")
        self.assertEqual(calls[0][3], None)

    def test_reads_cursor_trade_page_and_attaches_order_ownership(self) -> None:
        calls = []

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            return {
                "items": [trade()],
                "next_cursor": "NEXT",
                "has_more": True,
            }

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=request,
        )

        page = source.load_confirmed_trade_page(
            "ETF-OPTION-1", {"O-1": "ALPHA"}, cursor="A B", limit=20
        )

        self.assertEqual(page.fills[0].trade_id, "T-1")
        self.assertEqual(page.fills[0].strategy, "ALPHA")
        self.assertEqual(page.fills[0].quantity, -2)
        self.assertEqual(page.fills[0].price, Decimal("0.1234"))
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, "NEXT")
        self.assertTrue(
            calls[0][1].endswith(
                "/api/trades/page?account_id=ETF-OPTION-1&limit=20&cursor=A+B"
            )
        )

    def test_reads_cursor_order_page_for_recovery(self) -> None:
        calls = []

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            return {
                "items": [
                    {
                        "order_id": "O-1",
                        "client_order_id": "C-1",
                        "account_id": "ETF-OPTION-1",
                        "symbol": "9001",
                        "direction": "SELL",
                        "offset_flag": "OPEN",
                        "order_type": "LIMIT",
                        "limit_price": "0.1234",
                        "total_volume": 2,
                        "status": "FILLED",
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            }

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=request,
        )

        page = source.load_order_page(
            "ETF-OPTION-1", date(2026, 9, 18), cursor="A B", limit=20
        )

        self.assertEqual(page.orders[0].client_order_id, "C-1")
        self.assertEqual(page.orders[0].instrument, "9001")
        self.assertTrue(
            calls[0][1].endswith(
                "/api/orders/page?account_id=ETF-OPTION-1&"
                "trading_day=2026-09-18&limit=20&cursor=A+B"
            )
        )

    def test_trade_page_ignores_orders_not_owned_by_this_registry(self) -> None:
        unrelated = {**trade(), "order_id": "OTHER", "trade_id": "T-OTHER"}

        page = self._trade_page([unrelated, trade()], {"O-1": "ALPHA"})

        self.assertEqual([fill.trade_id for fill in page.fills], ["T-1"])

    def test_rejects_trade_without_saved_order_ownership(self) -> None:
        with self.assertRaisesRegex(SimTradingError, "no Alpha/Beta ownership"):
            normalize_confirmed_trade(trade(), "ETF-OPTION-1", {})

    @staticmethod
    def _trade_page(items, ownership):
        from sim_hedge.adapters.sim_trading import normalize_confirmed_trade_page

        return normalize_confirmed_trade_page(
            {"items": items, "next_cursor": None, "has_more": False},
            "ETF-OPTION-1",
            ownership,
        )

    def test_submits_exact_etf_option_request_and_requires_order_id(self) -> None:
        calls = []

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            return {"order_id": "O-1", "status": "ACCEPTED"}

        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=request,
        )
        body = {"client_order_id": "C-1", "symbol": "9001"}

        response = source.submit_etf_option_order(body)

        self.assertEqual(response["order_id"], "O-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(
            calls[0][1], "http://simulator.test/api/etf-options/orders"
        )
        self.assertEqual(calls[0][3], body)

    def test_submission_timeout_is_an_unknown_outcome(self) -> None:
        source = SimTradingPortfolioSource(
            "http://simulator.test",
            access_token="secret-token",
            request_json=lambda *args: (_ for _ in ()).throw(TimeoutError("late")),
        )

        with self.assertRaises(SimTradingUnknownOutcomeError):
            source.submit_etf_option_order({"client_order_id": "C-1"})


if __name__ == "__main__":
    unittest.main()
