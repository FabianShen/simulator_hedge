"""Local gRPC wrapper around the stateless reference hedge engine."""

import argparse
from collections.abc import Callable
from concurrent import futures
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import grpc

from hedge_service import ReferenceHedgeEngine
from hedge_engine.config import HedgeConfig
from hedge_service.protobuf_codec import (
    PROTOCOL_VERSION,
    health_response,
    request_from_proto,
    response_to_proto,
)
from hedging.v1 import hedging_pb2_grpc


DEFAULT_BIND = "127.0.0.1:50052"


class HedgeService(hedging_pb2_grpc.HedgeServiceServicer):
    def __init__(
        self,
        engine: Any,
        clock: Callable[[], datetime] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        if engine is None:
            raise ValueError("hedge engine must be configured explicitly")
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._protocol_version = protocol_version

    def Health(self, request, context):
        response = health_response()
        response.protocol_version = self._protocol_version
        return response

    def Propose(self, request, context):
        try:
            internal = request_from_proto(request)
            result = self._engine.propose(internal, created_at=self._clock())
            return response_to_proto(request.request_id, result)
        except (ValueError, FloatingPointError) as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except Exception as exc:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"hedge engine failed: {type(exc).__name__}",
            )


def create_server(
    bind: str = DEFAULT_BIND,
    *,
    engine: Any,
    clock: Callable[[], datetime] | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    max_workers: int = 4,
) -> tuple[grpc.Server, int]:
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    hedging_pb2_grpc.add_HedgeServiceServicer_to_server(
        HedgeService(
            engine=engine,
            clock=clock,
            protocol_version=protocol_version,
        ),
        server,
    )
    port = server.add_insecure_port(bind)
    if port == 0:
        raise RuntimeError(f"could not bind hedge server to {bind}")
    return server, port


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local hedge gRPC service")
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--capital", type=_positive_float, default=100_000_000.0)
    parser.add_argument(
        "--delta-entry-risk-band", type=_positive_float,
        default=30_000.0,
        help="scenario-risk entry band (used when --delta-limit is omitted)",
    )
    parser.add_argument("--delta-target-risk-band", type=_positive_float, default=10_000.0)
    parser.add_argument(
        "--gamma-entry-risk-band", type=_positive_float,
        default=10_000.0,
        help="scenario-risk entry band (used when --gamma-limit is omitted)",
    )
    parser.add_argument("--gamma-target-risk-band", type=_positive_float, default=10_000.0)
    parser.add_argument("--target-delta", type=_finite_float, default=0.0)
    parser.add_argument("--target-gamma", type=_finite_float, default=0.0)
    parser.add_argument(
        "--delta-limit", type=_positive_float, default=None,
        help="raw-Greek Delta tolerance around --target-delta",
    )
    parser.add_argument(
        "--gamma-limit", type=_positive_float, default=None,
        help="raw-Greek Gamma tolerance around --target-gamma",
    )
    parser.add_argument(
        "--position-limit", type=_nonnegative_int, default=300,
        help="max hedge contracts per instrument; 0 disables the limit (default 300)",
    )
    args = parser.parse_args()

    engine = ReferenceHedgeEngine(
        config=HedgeConfig(
            delta_entry_risk_band=args.delta_entry_risk_band,
            delta_target_risk_band=args.delta_target_risk_band,
            gamma_entry_risk_band=args.gamma_entry_risk_band,
            gamma_target_risk_band=args.gamma_target_risk_band,
            target_delta=args.target_delta,
            target_gamma=args.target_gamma,
            delta_limit=args.delta_limit,
            gamma_limit=args.gamma_limit,
            v2_position_limit=(args.position_limit or None),
        ),
        capital=args.capital,
    )
    server, port = create_server(
        args.bind, engine=engine, max_workers=args.workers
    )
    server.start()
    print(f"hedge service listening on {args.bind} (port {port})", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("stopping hedge service", flush=True)
        server.stop(grace=2).wait()


def _finite_float(value: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise argparse.ArgumentTypeError("value must be finite")
    return result


def _positive_float(value: str) -> float:
    result = _finite_float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "value must be a nonnegative integer"
        ) from exc
    if result < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return result


if __name__ == "__main__":
    main()
