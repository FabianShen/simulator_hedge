"""Translate versioned protobuf messages to and from the pure SABR engine."""

from datetime import datetime, timezone

from pricing.v1 import pricing_pb2
from pricing_engine import (
    OptionObservation,
    SabrPricingRequest,
    SabrPricingResponse,
)


ENGINE_NAME = "reference-python-sabr"
ENGINE_VERSION = "0.1.0"
PROTOCOL_VERSION = "pricing.v1"


def request_from_proto(request: pricing_pb2.PriceRequest) -> SabrPricingRequest:
    if not request.request_id:
        raise ValueError("request_id must not be empty")
    if request.configuration.model != pricing_pb2.PRICING_MODEL_SABR_BLACK_76:
        raise ValueError("only SABR/Black-76 requests are supported")
    if request.assumptions.day_count != pricing_pb2.DAY_COUNT_ACT_365_FIXED:
        raise ValueError("only ACT/365 Fixed is supported")
    if not request.options:
        raise ValueError("at least one option is required")

    expiries = {
        option.expiry.ToDatetime(tzinfo=timezone.utc)
        for option in request.options
    }
    if len(expiries) != 1:
        raise ValueError("one request must contain exactly one expiry")
    expiry = next(iter(expiries))
    as_of = request.as_of.ToDatetime(tzinfo=timezone.utc)
    time_to_expiry = (expiry - as_of).total_seconds() / (365 * 86400)
    if time_to_expiry <= 0:
        raise ValueError("option expiry must be after as_of")

    return SabrPricingRequest(
        spot=request.underlying.spot,
        time_to_expiry=time_to_expiry,
        rate=request.assumptions.risk_free_rate,
        dividend_yield=request.assumptions.dividend_yield,
        beta=request.configuration.sabr.beta,
        minimum_strikes=request.configuration.sabr.minimum_strikes,
        options=tuple(
            OptionObservation(
                instrument=option.instrument,
                option_type=_option_type(option.option_type),
                strike=option.strike,
                market_price=_market_price(option),
            )
            for option in request.options
        ),
    )


def response_to_proto(
    request_id: str,
    response: SabrPricingResponse,
    *,
    calculated_at: datetime,
) -> pricing_pb2.PriceResponse:
    if calculated_at.tzinfo is None:
        raise ValueError("calculated_at must be timezone-aware")

    message = pricing_pb2.PriceResponse(
        request_id=request_id,
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        model=pricing_pb2.PRICING_MODEL_SABR_BLACK_76,
    )
    message.calculated_at.FromDatetime(calculated_at.astimezone(timezone.utc))
    calibration = response.calibration
    message.sabr_calibration.CopyFrom(
        pricing_pb2.SabrCalibration(
            forward=calibration.forward,
            alpha=calibration.parameters.alpha,
            beta=calibration.parameters.beta,
            nu=calibration.parameters.nu,
            rho=calibration.parameters.rho,
            rmse=calibration.rmse,
            valid_strikes=calibration.valid_strikes,
        )
    )
    for result in response.results:
        item = message.results.add(
            instrument=result.instrument,
            status=_result_status(result.status),
            error=result.error or "",
        )
        _set_optional(item, "theoretical_price", result.theoretical_price)
        _set_optional(item, "implied_volatility", result.market_implied_volatility)
        _set_optional(item, "volatility_used", result.model_implied_volatility)
        _set_optional(item, "delta", result.delta)
        _set_optional(item, "gamma", result.gamma)
        _set_optional(item, "theta_per_year", result.theta_per_year)
        _set_optional(
            item,
            "vega_per_absolute_volatility",
            result.vega_per_absolute_volatility,
        )
        _set_optional(item, "rho_per_absolute_rate", result.rho_per_absolute_rate)
        if (
            result.market_implied_volatility is not None
            and result.model_implied_volatility is not None
        ):
            item.implied_volatility_error = (
                result.market_implied_volatility
                - result.model_implied_volatility
            )
    return message


def health_response() -> pricing_pb2.HealthResponse:
    return pricing_pb2.HealthResponse(
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )


def _market_price(option: pricing_pb2.OptionInput) -> float:
    if not option.HasField("market_price"):
        raise ValueError(f"market_price missing for {option.instrument}")
    return option.market_price


def _option_type(value: int) -> str:
    mapping = {
        pricing_pb2.OPTION_TYPE_CALL: "CALL",
        pricing_pb2.OPTION_TYPE_PUT: "PUT",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError("option type is unspecified or unsupported") from exc


def _result_status(value: str) -> int:
    return {
        "OK": pricing_pb2.RESULT_STATUS_OK,
        "INVALID_INPUT": pricing_pb2.RESULT_STATUS_INVALID_INPUT,
        "NO_SOLUTION": pricing_pb2.RESULT_STATUS_NO_SOLUTION,
    }.get(value, pricing_pb2.RESULT_STATUS_INTERNAL_ERROR)


def _set_optional(message, field: str, value: float | None) -> None:
    if value is not None:
        setattr(message, field, value)
