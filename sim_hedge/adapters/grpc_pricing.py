"""gRPC client adapter for the versioned external pricing service."""

from datetime import timezone
from typing import Any, Mapping

import grpc
from google.protobuf import json_format

from pricing.v1 import pricing_pb2, pricing_pb2_grpc
from sim_hedge.domain.pricing import (
    OptionValuation,
    PricingBatch,
    PricingServiceHealth,
    SabrFit,
)


EXPECTED_PROTOCOL_VERSION = "pricing.v1"


class PricingServiceError(RuntimeError):
    """The external pricing service is unavailable or rejected a request."""


class GrpcPricingClient:
    def __init__(
        self,
        target: str = "127.0.0.1:50051",
        *,
        timeout: float = 1.0,
        channel: grpc.Channel | None = None,
    ) -> None:
        if not target:
            raise ValueError("target must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._channel = channel or grpc.insecure_channel(target)
        self._owns_channel = channel is None
        self._stub = pricing_pb2_grpc.PricingServiceStub(self._channel)

    def health(self) -> PricingServiceHealth:
        from google.protobuf.empty_pb2 import Empty

        try:
            response = self._stub.Health(Empty(), timeout=self._timeout)
        except grpc.RpcError as exc:
            raise _service_error("health check", exc) from exc
        if response.protocol_version != EXPECTED_PROTOCOL_VERSION:
            raise PricingServiceError(
                f"protocol mismatch: expected {EXPECTED_PROTOCOL_VERSION}, "
                f"received {response.protocol_version or '<empty>'}"
            )
        return PricingServiceHealth(
            engine_name=response.engine_name,
            engine_version=response.engine_version,
            protocol_version=response.protocol_version,
        )

    def price(self, request: Mapping[str, Any]) -> PricingBatch:
        message = pricing_pb2.PriceRequest()
        try:
            payload = dict(request)
            # Book fields travel in the recorded pricing snapshot for hedge
            # request construction; the pricing protocol does not consume them.
            payload["options"] = [
                {
                    key: value
                    for key, value in option.items()
                    if key not in {"bid", "ask", "bidSize", "askSize"}
                }
                for option in request.get("options", ())
            ]
            json_format.ParseDict(payload, message)
        except (json_format.ParseError, ValueError, TypeError) as exc:
            raise PricingServiceError(f"invalid pricing request: {exc}") from exc
        try:
            response = self._stub.Price(message, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise _service_error("pricing request", exc) from exc
        if response.request_id != message.request_id:
            raise PricingServiceError(
                f"response request_id mismatch: {response.request_id!r}"
            )
        return _batch_from_proto(response)

    def close(self) -> None:
        if self._owns_channel:
            self._channel.close()

    def __enter__(self) -> "GrpcPricingClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _batch_from_proto(response: pricing_pb2.PriceResponse) -> PricingBatch:
    if not response.HasField("sabr_calibration"):
        raise PricingServiceError("pricing response has no SABR calibration")
    calibration = response.sabr_calibration
    return PricingBatch(
        request_id=response.request_id,
        calculated_at=response.calculated_at.ToDatetime(tzinfo=timezone.utc),
        engine_name=response.engine_name,
        engine_version=response.engine_version,
        model=pricing_pb2.PricingModel.Name(response.model).removeprefix(
            "PRICING_MODEL_"
        ),
        calibration=SabrFit(
            forward=calibration.forward,
            alpha=calibration.alpha,
            beta=calibration.beta,
            nu=calibration.nu,
            rho=calibration.rho,
            rmse=calibration.rmse,
            valid_strikes=calibration.valid_strikes,
        ),
        results=tuple(_valuation_from_proto(item) for item in response.results),
    )


def _valuation_from_proto(item: pricing_pb2.OptionResult) -> OptionValuation:
    return OptionValuation(
        instrument=item.instrument,
        status=pricing_pb2.ResultStatus.Name(item.status).removeprefix(
            "RESULT_STATUS_"
        ),
        error=item.error or None,
        theoretical_price=_optional(item, "theoretical_price"),
        market_implied_volatility=_optional(item, "implied_volatility"),
        model_implied_volatility=_optional(item, "volatility_used"),
        implied_volatility_error=_optional(item, "implied_volatility_error"),
        delta=_optional(item, "delta"),
        gamma=_optional(item, "gamma"),
        theta_per_year=_optional(item, "theta_per_year"),
        vega_per_absolute_volatility=_optional(
            item,
            "vega_per_absolute_volatility",
        ),
        rho_per_absolute_rate=_optional(item, "rho_per_absolute_rate"),
    )


def _optional(message, field: str) -> float | None:
    return getattr(message, field) if message.HasField(field) else None


def _service_error(operation: str, error: grpc.RpcError) -> PricingServiceError:
    code = error.code().name if error.code() is not None else "UNKNOWN"
    detail = error.details() or "no details"
    return PricingServiceError(f"{operation} failed [{code}]: {detail}")
