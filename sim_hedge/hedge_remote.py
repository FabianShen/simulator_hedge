"""Send a recorded hedge request to the external gRPC service."""

import argparse
import json
from pathlib import Path

from sim_hedge.adapters.grpc_hedging import GrpcHedgeClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge a recorded request over gRPC")
    parser.add_argument("request", type=Path)
    parser.add_argument("--target", default="127.0.0.1:50052")
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    request = json.loads(args.request.read_text(encoding="utf-8"))
    with GrpcHedgeClient(args.target, timeout=args.timeout) as client:
        health = client.health()
        print(
            f"connected to {health['engine_name']} {health['engine_version']} "
            f"({health['protocol_version']})",
            flush=True,
        )
        response = client.propose(request)
    rendered = json.dumps(response, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote hedge proposal: {args.output}")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
