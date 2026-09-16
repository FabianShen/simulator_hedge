"""Stream normalized live quotes with ``python -m sim_hedge``."""

import argparse
import os

from sim_hedge.adapters.ymm_live import YmmLiveDataSource


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream live YMM market ticks")
    parser.add_argument("instruments", nargs="*", default=["159915.XSHE"])
    parser.add_argument(
        "--mode", choices=("lan", "TS"), default=os.getenv("YMM_LIVE_MODE", "lan")
    )
    parser.add_argument(
        "--max-quotes", type=int, default=0, help="stop after N quotes; 0 runs continuously"
    )
    args = parser.parse_args()

    token = os.getenv("YMM_LIVE_DATA_TOKEN") or os.getenv("LIVE_TOKEN")
    if not token:
        parser.error("set YMM_LIVE_DATA_TOKEN or LIVE_TOKEN before running")

    source = YmmLiveDataSource(token=token, mode=args.mode, instruments=args.instruments)
    count = 0

    def display(quote) -> None:
        nonlocal count
        count += 1
        print(
            f"{quote.observed_at.isoformat()} {quote.instrument} "
            f"last={quote.last} bid={quote.bid} ask={quote.ask}",
            flush=True,
        )
        if args.max_quotes > 0 and count >= args.max_quotes:
            source.stop()

    print(f"subscribing to: {', '.join(source.channels)}", flush=True)
    try:
        source.run(display)
    except KeyboardInterrupt:
        source.stop()
    except Exception as exc:
        raise SystemExit(f"live feed failed: {type(exc).__name__}: {exc}") from exc
    finally:
        print(f"feed health: {source.health}", flush=True)


if __name__ == "__main__":
    main()
