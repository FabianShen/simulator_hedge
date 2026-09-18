"""Send a recorded pricing request to the external gRPC service."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from sim_hedge.adapters.grpc_pricing import GrpcPricingClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Price a recorded request over gRPC")
    parser.add_argument("request", type=Path)
    parser.add_argument("--target", default="127.0.0.1:50051")
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args()

    request = json.loads(args.request.read_text(encoding="utf-8"))
    with GrpcPricingClient(args.target, timeout=args.timeout) as client:
        health = client.health()
        print(
            f"connected to {health.engine_name} {health.engine_version} "
            f"({health.protocol_version})",
            flush=True,
        )
        response = client.price(request)
    print(json.dumps(asdict(response), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
