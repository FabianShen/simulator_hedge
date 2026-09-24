import unittest
from datetime import date
from types import SimpleNamespace

from sim_hedge.adapters.ymm_reference import ReferenceDataError, YmmReferenceDataSource
from sim_hedge.domain.contract import OptionContract, OptionType


class FakeInstrument:
    def __init__(self, values: dict) -> None:
        self.values = values

    def to_dict(self) -> dict:
        return dict(self.values)


class FakeSdk:
    def __init__(self, instruments) -> None:
        self.init_calls = []
        self.contract_calls = []
        self.instrument_calls = []
        self.closed = False
        self.options = SimpleNamespace(get_contracts=self.get_contracts)
        self._instruments = instruments

    def init(self, *, token: str, mode: str) -> None:
        self.init_calls.append((token, mode))

    def get_contracts(self, underlying: str, *, trading_date: date):
        self.contract_calls.append((underlying, trading_date))
        return ["90000001", "90000002"]

    def instruments(self, instrument_ids):
        self.instrument_calls.append(instrument_ids)
        return self._instruments

    def close(self) -> None:
        self.closed = True


class YmmReferenceDataSourceTests(unittest.TestCase):
    def test_loads_normalized_option_contracts(self) -> None:
        sdk = FakeSdk([
            FakeInstrument({
                "order_book_id": "90000001",
                "option_type": "C",
                "strike_price": 3.3,
                "maturity_date": "2026-09-23",
                "contract_multiplier": 10000,
                "tick_size": 0.0001,
            }),
            FakeInstrument({
                "order_book_id": "90000002",
                "option_type": "P",
                "strike_price": 3.3,
                "maturity_date": "2026-09-23",
                "contract_multiplier": 10000,
                "tick_size": 0.0001,
            }),
        ])
        asof = date(2026, 9, 17)

        with YmmReferenceDataSource("secret", sdk=sdk) as source:
            contracts = source.load_option_chain("159915.XSHE", asof)

        self.assertEqual(sdk.init_calls, [("secret", "lan")])
        self.assertEqual(sdk.contract_calls, [("159915.XSHE", asof)])
        self.assertEqual(
            contracts,
            [
                OptionContract(
                    "90000001", "159915.XSHE", OptionType.CALL, 3.3,
                    date(2026, 9, 23), 10000, 0.0001,
                ),
                OptionContract(
                    "90000002", "159915.XSHE", OptionType.PUT, 3.3,
                    date(2026, 9, 23), 10000, 0.0001,
                ),
            ],
        )
        self.assertTrue(sdk.closed)

    def test_rejects_unknown_option_type(self) -> None:
        sdk = FakeSdk([FakeInstrument({
            "order_book_id": "90000001",
            "option_type": "UNKNOWN",
            "strike_price": 3.3,
            "maturity_date": "2026-09-23",
            "contract_multiplier": 10000,
            "tick_size": 0.0001,
        })])

        with YmmReferenceDataSource("secret", sdk=sdk) as source:
            with self.assertRaisesRegex(ReferenceDataError, "invalid option instrument"):
                source.load_option_chain("159915.XSHE", date(2026, 9, 17))


if __name__ == "__main__":
    unittest.main()
