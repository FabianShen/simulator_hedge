"""Run the reference SABR engine against a saved JSON request."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from pricing_engine import OptionObservation, SabrPricingEngine, SabrPricingRequest


DEFAULT_REQUEST = Path(__file__).with_name("examples") / "sabr_request.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Price a saved option smile offline")
    parser.add_argument("request", nargs="?", type=Path, default=DEFAULT_REQUEST)
    args = parser.parse_args()

    payload = json.loads(args.request.read_text(encoding="utf-8"))
    request = SabrPricingRequest(
        spot=float(payload["spot"]),
        time_to_expiry=float(payload["time_to_expiry"]),
        rate=float(payload["rate"]),
        dividend_yield=float(payload["dividend_yield"]),
        beta=float(payload.get("beta", 0.5)),
        options=tuple(
            OptionObservation(
                instrument=option["instrument"],
                option_type=option["option_type"],
                strike=float(option["strike"]),
                market_price=float(option["market_price"]),
            )
            for option in payload["options"]
        ),
    )
    response = SabrPricingEngine().price(request)
    print(json.dumps(asdict(response), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
