"""Run the reference SABR engine against a saved JSON request."""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path

from pricing_engine import OptionObservation, SabrPricingEngine, SabrPricingRequest


DEFAULT_REQUEST = Path(__file__).with_name("examples") / "sabr_request.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Price a saved option smile offline")
    parser.add_argument("request", nargs="?", type=Path, default=DEFAULT_REQUEST)
    args = parser.parse_args()

    request = load_request(args.request)
    response = SabrPricingEngine().price(request)
    print(json.dumps(asdict(response), ensure_ascii=False, indent=2))


def load_request(path: Path) -> SabrPricingRequest:
    """Load either a recorded protocol request or the original compact fixture."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if "requestId" in payload:
        return _load_protocol_request(payload)
    return _load_compact_request(payload)


def _load_compact_request(payload: dict) -> SabrPricingRequest:
    return SabrPricingRequest(
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


def _load_protocol_request(payload: dict) -> SabrPricingRequest:
    if payload["configuration"]["model"] != "PRICING_MODEL_SABR_BLACK_76":
        raise ValueError("only PRICING_MODEL_SABR_BLACK_76 is supported")
    if payload["assumptions"]["dayCount"] != "DAY_COUNT_ACT_365_FIXED":
        raise ValueError("only DAY_COUNT_ACT_365_FIXED is supported")

    as_of = _parse_timestamp(payload["asOf"])
    expiries = {_parse_timestamp(option["expiry"]) for option in payload["options"]}
    if len(expiries) != 1:
        raise ValueError("one pricing request must contain exactly one expiry")
    expiry = next(iter(expiries))
    time_to_expiry = (expiry - as_of).total_seconds() / (365 * 86400)
    if time_to_expiry <= 0:
        raise ValueError("option expiry must be after asOf")

    sabr = payload["configuration"]["sabr"]
    return SabrPricingRequest(
        spot=float(payload["underlying"]["spot"]),
        time_to_expiry=time_to_expiry,
        rate=float(payload["assumptions"]["riskFreeRate"]),
        dividend_yield=float(payload["assumptions"]["dividendYield"]),
        beta=float(sabr["beta"]),
        minimum_strikes=int(sabr["minimumStrikes"]),
        options=tuple(
            OptionObservation(
                instrument=option["instrument"],
                option_type=_option_type(option["optionType"]),
                strike=float(option["strike"]),
                market_price=float(option["marketPrice"]),
            )
            for option in payload["options"]
        ),
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("protocol timestamps must contain a UTC offset")
    return parsed


def _option_type(value: str) -> str:
    mapping = {
        "OPTION_TYPE_CALL": "CALL",
        "OPTION_TYPE_PUT": "PUT",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unsupported option type: {value}") from exc


if __name__ == "__main__":
    main()
