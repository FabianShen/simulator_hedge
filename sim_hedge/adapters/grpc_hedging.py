"""gRPC client adapter for the versioned external hedge service."""

from datetime import timezone
from typing import Any, Mapping

import grpc
from google.protobuf import json_format

from hedge_engine import HEDGE_PROPOSAL_VERSION, validate_hedge_proposal
from hedging.v1 import hedging_pb2, hedging_pb2_grpc


EXPECTED_PROTOCOL_VERSION = "hedging.v1"


class HedgeServiceError(RuntimeError):
    """The external hedge service is unavailable or rejected a request."""


class GrpcHedgeClient:
    def __init__(
        self,
        target: str = "127.0.0.1:50052",
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
        self._stub = hedging_pb2_grpc.HedgeServiceStub(self._channel)

    def health(self) -> dict[str, str]:
        from google.protobuf.empty_pb2 import Empty

        try:
            response = self._stub.Health(Empty(), timeout=self._timeout)
        except grpc.RpcError as exc:
            raise _service_error("health check", exc) from exc
        if response.protocol_version != EXPECTED_PROTOCOL_VERSION:
            raise HedgeServiceError(
                f"protocol mismatch: expected {EXPECTED_PROTOCOL_VERSION}, "
                f"received {response.protocol_version or '<empty>'}"
            )
        if response.proposal_protocol_version != HEDGE_PROPOSAL_VERSION:
            raise HedgeServiceError(
                "hedge proposal protocol mismatch: expected "
                f"{HEDGE_PROPOSAL_VERSION}, received "
                f"{response.proposal_protocol_version or '<empty>'}"
            )
        return {
            "engine_name": response.engine_name,
            "engine_version": response.engine_version,
            "protocol_version": response.protocol_version,
            "proposal_protocol_version": response.proposal_protocol_version,
        }

    def propose(self, request: Mapping[str, Any]) -> dict[str, Any]:
        message = hedging_pb2.HedgeRequest()
        try:
            json_format.ParseDict(dict(request), message)
        except (json_format.ParseError, ValueError, TypeError) as exc:
            raise HedgeServiceError(f"invalid hedge request: {exc}") from exc
        try:
            response = self._stub.Propose(message, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise _service_error("hedge request", exc) from exc
        if response.request_id != message.request_id:
            raise HedgeServiceError(
                f"response request_id mismatch: {response.request_id!r}"
            )
        proposal = _proposal_from_proto(response)
        validate_hedge_proposal(
            proposal,
            pricing_request_id=message.source_pricing_request_id,
            account_id=message.account_id,
            base_ledger_revision=message.base_strategy_ledger_revision,
            confirmed_beta_positions=dict(message.confirmed_beta_positions),
        )
        return proposal

    def close(self) -> None:
        if self._owns_channel:
            self._channel.close()

    def __enter__(self) -> "GrpcHedgeClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _proposal_from_proto(response: hedging_pb2.HedgeProposal) -> dict[str, Any]:
    if not response.HasField("source_market_as_of"):
        raise HedgeServiceError("hedge response has no source_market_as_of")
    return {
        "protocol_version": response.protocol_version,
        "proposal_id": response.proposal_id,
        "proposal_type": response.proposal_type,
        "created_at": response.created_at.ToDatetime(
            tzinfo=timezone.utc
        ).isoformat(),
        "source_pricing_request_id": response.source_pricing_request_id,
        "source_market_as_of": response.source_market_as_of.ToDatetime(
            tzinfo=timezone.utc
        ).isoformat(),
        "account_id": response.account_id,
        "base_strategy_ledger_revision": response.base_strategy_ledger_revision,
        "decision_engine": {
            "name": response.engine_name,
            "version": response.engine_version,
        },
        "confirmed_beta_positions": dict(response.confirmed_beta_positions),
        "incremental_trades": dict(response.incremental_trades),
        "target_beta_positions": dict(response.target_beta_positions),
        "orders_generated": response.orders_generated,
        "hedge_pair": list(response.hedge_pair),
        "risk": {
            "alpha": _greeks(response.alpha_risk),
            "confirmed_beta": _greeks(response.confirmed_beta_risk),
            "portfolio": _greeks(response.portfolio_risk),
            "at_target_beta": _greeks(response.risk_at_target_beta),
        },
        "normalized_residual": response.normalized_residual,
    }


def _greeks(value) -> dict[str, float]:
    return {
        "delta": value.delta,
        "gamma": value.gamma,
        "vega": value.vega,
        "theta": value.theta,
    }


def _service_error(operation: str, error: grpc.RpcError) -> HedgeServiceError:
    code = error.code().name if error.code() is not None else "UNKNOWN"
    detail = error.details() or "no details"
    return HedgeServiceError(f"{operation} failed [{code}]: {detail}")
