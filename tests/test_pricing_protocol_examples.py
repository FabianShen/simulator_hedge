import json
import unittest
from datetime import datetime
from pathlib import Path


EXAMPLES = (
    Path(__file__).parents[1]
    / "protocols"
    / "pricing"
    / "v1"
    / "examples"
)


class PricingProtocolExampleTests(unittest.TestCase):
    def test_request_example_has_explicit_units_and_identifiers(self) -> None:
        request = json.loads(
            (EXAMPLES / "price_request.json").read_text(encoding="utf-8")
        )

        self.assertTrue(request["requestId"])
        self.assertGreater(request["underlying"]["spot"], 0)
        self.assertEqual(
            request["assumptions"]["dayCount"],
            "DAY_COUNT_ACT_365_FIXED",
        )
        self.assertTrue(request["options"])
        self.assertEqual(
            request["configuration"]["model"],
            "PRICING_MODEL_SABR_BLACK_76",
        )
        self.assertGreaterEqual(
            len({option["strike"] for option in request["options"]}),
            request["configuration"]["sabr"]["minimumStrikes"],
        )
        for option in request["options"]:
            self.assertGreater(option["strike"], 0)
            self.assertGreater(option["contractMultiplier"], 0)
            self.assertGreater(option["priceTick"], 0)
            self.assertGreater(
                _parse_utc(option["expiry"]),
                _parse_utc(request["asOf"]),
            )

    def test_response_example_matches_request(self) -> None:
        request = json.loads(
            (EXAMPLES / "price_request.json").read_text(encoding="utf-8")
        )
        response = json.loads(
            (EXAMPLES / "price_response.json").read_text(encoding="utf-8")
        )

        self.assertEqual(response["requestId"], request["requestId"])
        self.assertEqual(
            {result["instrument"] for result in response["results"]},
            {option["instrument"] for option in request["options"]},
        )
        self.assertGreaterEqual(response["sabrCalibration"]["validStrikes"], 3)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed


if __name__ == "__main__":
    unittest.main()
