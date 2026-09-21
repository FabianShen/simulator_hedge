"""Local gRPC wrapper around the stateless reference hedge engine."""

import argparse
from collections.abc import Callable
from concurrent import futures
from datetime import datetime, timezone
from typing import Any

import grpc

from hedge_service import ReferenceHedgeEngine
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
        engine: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._engine = engine or ReferenceHedgeEngine()
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
    engine: Any | None = None,
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
    args = parser.parse_args()

    server, port = create_server(args.bind, max_workers=args.workers)
    server.start()
    print(f"hedge service listening on {args.bind} (port {port})", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("stopping hedge service", flush=True)
        server.stop(grace=2).wait()


if __name__ == "__main__":
    main()
