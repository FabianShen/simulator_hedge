"""Stream normalized live quotes with ``python -m sim_hedge``."""

import argparse
from datetime import date, datetime, timedelta, timezone
import os

from sim_hedge.adapters.ymm_live import YmmLiveDataSource
from sim_hedge.adapters.ymm_reference import YmmReferenceDataSource
from sim_hedge.market_state import MarketState
from sim_hedge.option_chain import summarize


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream live YMM market ticks")
    parser.add_argument("instruments", nargs="*", default=["159915.XSHE"])
    parser.add_argument("--mode", choices=("lan", "TS"), default=os.getenv("LIVE_MODE", "lan"))
    parser.add_argument("--max-quotes", type=int, default=0, help="stop after N quotes; 0 no stop")
    parser.add_argument(
        "--option-chain",
        metavar="UNDERLYING",
        help="print active option metadata and exit without starting the live feed",
    )
    args = parser.parse_args()

    if args.option_chain:
        print_option_chain(args.option_chain, args.mode, parser)
        return

    token = os.getenv("LIVE_TOKEN")
    if not token:
        parser.error("set LIVE_TOKEN before running")

    source = YmmLiveDataSource(token=token, mode=args.mode, instruments=args.instruments)
    market_state = MarketState(args.instruments)
    count = 0

    def display(quote) -> None:
        nonlocal count
        market_state.apply_quote(quote)
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
        readiness = market_state.readiness(
            now=datetime.now(timezone.utc),
            max_age=timedelta(seconds=5),
            feed_unsafe=source.health.data_unsafe,
        )
        print(f"market state: {readiness}", flush=True)


def print_option_chain(underlying: str, mode: str, parser: argparse.ArgumentParser) -> None:
    token = os.getenv("DATA_TOKEN")
    if not token:
        parser.error("set DATA_TOKEN before requesting the option chain")

    with YmmReferenceDataSource(token=token, mode=mode) as source:
        contracts = source.load_option_chain(underlying, date.today())

    summaries = summarize(contracts)
    print(f"underlying: {underlying}")
    print(f"contracts:  {len(contracts)}")
    print(f"maturities: {len(summaries)}")
    for summary in summaries:
        print(
            f"{summary.maturity.isoformat()}  calls={summary.calls} puts={summary.puts} "
            f"strikes={summary.minimum_strike:g}-{summary.maximum_strike:g}"
        )


if __name__ == "__main__":
    main()
