"""Stream normalized live quotes with ``python -m sim_hedge``."""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import DecimalException
import json
import os
from pathlib import Path

from sim_hedge.adapters.ymm_live import YmmLiveDataSource
from sim_hedge.adapters.ymm_reference import YmmReferenceDataSource
from sim_hedge.adapters.grpc_hedging import GrpcHedgeClient, HedgeServiceError
from sim_hedge.adapters.grpc_pricing import GrpcPricingClient, PricingServiceError
from sim_hedge.alpha_market import build_alpha_market_snapshot
from sim_hedge.config import load_env_file
from sim_hedge.domain import OptionContract
from sim_hedge.market_monitor import MarketMonitor
from sim_hedge.market_state import MarketState
from sim_hedge.live_risk import write_live_risk_snapshot
from sim_hedge.hedge_request import build_hedge_request
from sim_hedge.option_chain import subscription, summarize
from sim_hedge.pricing_request import (
    PricingRequestError,
    PricingRequestPolicy,
    build_pricing_request,
    held_valuation_contracts,
    record_pricing_request,
)
from sim_hedge.pricing_worker import ContinuousPricingWorker
from sim_hedge.strategy_universe import StrategyUniverse, select_strategy_universe
from sim_hedge.strategy_ledger import ledger_from_payload


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Stream live YMM market ticks")
    parser.add_argument("underlying", nargs="?", default="159915.XSHE")
    parser.add_argument("--mode", choices=("lan", "TS"), default=os.getenv("LIVE_MODE", "lan"))
    parser.add_argument("--max-quotes", type=int, default=0, help="stop after N quotes; 0 no stop")
    parser.add_argument(
        "--expiry",
        metavar="INDEX",
        type=int,
        default=0,
        help="zero-based maturity to use for the strategy; default: DTE",
    )
    parser.add_argument(
        "--strike-wings",
        type=int,
        default=2,
        help="strike levels on each side of ATM; default: 2",
    )
    parser.add_argument(
        "--snapshot",
        default="outputs/market_state.json",
        help="diagnostic JSON path; use an empty string to disable",
    )
    parser.add_argument(
        "--record-pricing",
        metavar="PATH",
        help="record the first ready pricing request for offline replay",
    )
    parser.add_argument(
        "--stop-after-recording",
        action="store_true",
        help="stop the live feed immediately after --record-pricing succeeds",
    )
    parser.add_argument("--risk-free-rate", type=float, default=0.015)
    parser.add_argument("--dividend-yield", type=float, default=0.0)
    parser.add_argument("--sabr-beta", type=float, default=0.5)
    parser.add_argument(
        "--pricing-target",
        default=os.getenv("PRICING_TARGET", ""),
        metavar="HOST:PORT",
        help="enable continuous external pricing; example: 127.0.0.1:50051",
    )
    parser.add_argument("--pricing-interval", type=float, default=1.0)
    parser.add_argument("--pricing-timeout", type=float, default=0.5)
    parser.add_argument("--pricing-max-age", type=float, default=2.0)
    parser.add_argument(
        "--pricing-output",
        default="outputs/live-pricing-request.json",
        help="atomic latest pricing input for audit and automatic trading",
    )
    parser.add_argument(
        "--alpha-market-output",
        default="outputs/live-alpha-market.json",
        help="latest available nearest-expiry chain for automatic Alpha initialization",
    )
    parser.add_argument(
        "--alpha-collection-seconds",
        type=float,
        default=10.0,
        help="collect opening ticks before publishing the available Alpha chain",
    )
    parser.add_argument(
        "--hedge-target",
        default=os.getenv("HEDGE_TARGET", ""),
        metavar="HOST:PORT",
        help="use the external hedge service; example: 127.0.0.1:50052",
    )
    parser.add_argument("--hedge-timeout", type=float, default=0.5)
    parser.add_argument(
        "--strategy-ledger",
        metavar="PATH",
        help="publish live Greeks and a Beta target from this confirmed ledger",
    )
    parser.add_argument(
        "--risk-output",
        default="outputs/live_risk.json",
        help="atomic live-risk output used with --strategy-ledger",
    )
    parser.add_argument(
        "--expiry-time",
        type=parse_clock_time,
        default=parse_clock_time("15:00"),
        metavar="HH:MM",
        help="exchange-local expiry time; verify for the traded contract",
    )
    parser.add_argument(
        "--check-options",
        metavar="UNDERLYING",
        help="print active option metadata without live",
    )
    args = parser.parse_args()
    if args.expiry < 0:
        parser.error("--expiry must not be negative")
    if args.strike_wings < 0:
        parser.error("--strike-wings must not be negative")
    if not 0 <= args.sabr_beta <= 1:
        parser.error("--sabr-beta must be between zero and one")
    if args.pricing_interval <= 0:
        parser.error("--pricing-interval must be positive")
    if args.pricing_timeout <= 0:
        parser.error("--pricing-timeout must be positive")
    if args.pricing_max_age <= 0:
        parser.error("--pricing-max-age must be positive")
    if args.hedge_timeout <= 0:
        parser.error("--hedge-timeout must be positive")
    if args.alpha_collection_seconds < 0:
        parser.error("--alpha-collection-seconds must not be negative")
    if args.stop_after_recording and not args.record_pricing:
        parser.error("--stop-after-recording requires --record-pricing")
    # Review option chain without activate live feed
    if args.check_options:
        contracts = load_option_chain(args.check_options, args.mode, parser)
        print_option_chain(args.check_options, contracts)
        return
    if args.strategy_ledger and not args.pricing_target:
        parser.error("--strategy-ledger requires --pricing-target")
    if args.strategy_ledger and not args.hedge_target:
        parser.error("--strategy-ledger requires --hedge-target")
    if args.hedge_target and not args.strategy_ledger:
        parser.error("--hedge-target requires --strategy-ledger")

    live_token = os.getenv("LIVE_TOKEN")
    if not live_token:
        parser.error("set LIVE_TOKEN before running")

    contracts = load_option_chain(args.underlying, args.mode, parser)
    instruments = subscription(args.underlying, contracts)
    source = YmmLiveDataSource(
        token=live_token,
        mode=args.mode,
        instruments=instruments,
    )
    # The first underlying quote supplies the spot needed to select ATM strikes.
    market_state = MarketState([args.underlying])
    count = 0
    universe: StrategyUniverse | None = None
    pricing_recorded = False
    pricing_record_error = "strategy universe not selected"
    alpha_market_recorded_at: datetime | None = None
    alpha_collection_started_at = datetime.now(timezone.utc)
    pricing_policy = PricingRequestPolicy(
        risk_free_rate=args.risk_free_rate,
        dividend_yield=args.dividend_yield,
        beta=args.sabr_beta,
        expiry_time=args.expiry_time,
    )
    pricing_client: GrpcPricingClient | None = None
    hedge_client: GrpcHedgeClient | None = None
    pricing_worker: ContinuousPricingWorker | None = None

    if args.pricing_target:
        pricing_client = GrpcPricingClient(
            args.pricing_target,
            timeout=args.pricing_timeout,
        )
        try:
            health = pricing_client.health()
        except PricingServiceError as exc:
            pricing_client.close()
            raise SystemExit(f"pricing service unavailable: {exc}") from exc
        print(
            f"pricing service: {health.engine_name} {health.engine_version} "
            f"({health.protocol_version})",
            flush=True,
        )
        if args.hedge_target:
            hedge_client = GrpcHedgeClient(
                args.hedge_target,
                timeout=args.hedge_timeout,
            )
            try:
                hedge_health = hedge_client.health()
            except HedgeServiceError as exc:
                hedge_client.close()
                pricing_client.close()
                raise SystemExit(f"hedge service unavailable: {exc}") from exc
            print(
                f"hedge service: {hedge_health['engine_name']} "
                f"{hedge_health['engine_version']} "
                f"({hedge_health['protocol_version']})",
                flush=True,
            )

        def make_pricing_request(request_id, as_of):
            if universe is None:
                raise PricingRequestError("strategy universe not selected")
            held_contracts = ()
            if args.strategy_ledger and Path(args.strategy_ledger).exists():
                ledger_payload = json.loads(
                    Path(args.strategy_ledger).read_text(encoding="utf-8")
                )
                if not isinstance(ledger_payload, dict):
                    raise PricingRequestError("strategy ledger is not an object")
                ledger = ledger_from_payload(ledger_payload)
                held_contracts = held_valuation_contracts(ledger, contracts)
            return build_pricing_request(
                request_id=request_id,
                as_of=as_of,
                market_state=market_state,
                universe=universe,
                policy=pricing_policy,
                hedge_contracts=contracts,
                valuation_contracts=held_contracts,
                feed_unsafe=(
                    source.health.data_unsafe or source.health.state != "running"
                ),
            )

        def display_pricing(request, result) -> None:
            if args.pricing_output:
                record_pricing_request(args.pricing_output, dict(request))
            if args.strategy_ledger:
                try:
                    ledger_payload = json.loads(
                        Path(args.strategy_ledger).read_text(encoding="utf-8")
                    )
                    if not isinstance(ledger_payload, dict):
                        raise ValueError("strategy ledger is not an object")
                    ledger = ledger_from_payload(ledger_payload)
                    assert hedge_client is not None
                    risk = hedge_client.propose(
                        build_hedge_request(
                            request, result, ledger,
                            hedge_candidates=(
                                str(option["instrument"])
                                for option in request["options"]
                                if "marketPrice" in option
                            ),
                        )
                    )
                    risk = {
                        **risk,
                        "status": "READY",
                        "pricing_calculated_at": result.calculated_at.isoformat(),
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    }
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    DecimalException,
                    json.JSONDecodeError,
                    HedgeServiceError,
                ) as exc:
                    risk = {
                        "status": "NOT_READY",
                        "source_pricing_request_id": result.request_id,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                        "orders_generated": False,
                    }
                    write_live_risk_snapshot(args.risk_output, risk)
                    print(f"live risk not ready: {exc}", flush=True)
                    return
                write_live_risk_snapshot(args.risk_output, risk)
                portfolio_risk = risk["risk"]["portfolio"]
                target = risk["target_beta_positions"]
                print(
                    f"live risk request={result.request_id} "
                    f"delta={portfolio_risk['delta']:.6g} "
                    f"gamma={portfolio_risk['gamma']:.6g} "
                    f"desired_beta={target}",
                    flush=True,
                )
                return
            print(
                f"pricing ready request={result.request_id} "
                f"options={len(result.results)} "
                f"rmse={result.calibration.rmse:.8f}",
                flush=True,
            )

        pricing_worker = ContinuousPricingWorker(
            pricing_client,
            make_pricing_request,
            interval=args.pricing_interval,
            max_result_age=timedelta(seconds=args.pricing_max_age),
            on_priced=display_pricing,
        )

    def on_quote(quote) -> None:
        nonlocal count, universe, pricing_recorded, pricing_record_error
        nonlocal alpha_market_recorded_at
        if args.max_quotes > 0 and count >= args.max_quotes:
            return
        market_state.apply_quote(quote)
        if quote.instrument == args.underlying and universe is None:
            spot = quote_price(quote)
            # this selects the available positions
            universe = select_strategy_universe(
                contracts,
                spot,
                expiry_index=args.expiry,
                strike_wings=args.strike_wings,
            )
            market_state.set_required([args.underlying, *universe.instruments])
            print(
                f"strategy universe: maturity={universe.maturity.isoformat()} "
                f"spot={spot:g} center={universe.center_strike:g} "
                f"strikes={len(universe.strikes)} "
                f"options={len(universe.contracts)}",
                flush=True,
            )
        if args.record_pricing and universe is not None and not pricing_recorded:
            as_of = datetime.now(timezone.utc)
            try:
                request = build_pricing_request(
                    request_id=f"live-{as_of.strftime('%Y%m%dT%H%M%S.%fZ')}",
                    as_of=as_of,
                    market_state=market_state,
                    universe=universe,
                    policy=pricing_policy,
                    hedge_contracts=contracts,
                    feed_unsafe=source.health.data_unsafe,
                )
            except PricingRequestError as exc:
                pricing_record_error = str(exc)
            else:
                record_pricing_request(args.record_pricing, request)
                pricing_recorded = True
                pricing_record_error = ""
                print(
                    f"recorded pricing request: {args.record_pricing}",
                    flush=True,
                )
                if args.stop_after_recording:
                    source.stop()
        now = datetime.now(timezone.utc)
        if (
            args.alpha_market_output
            and now - alpha_collection_started_at
            >= timedelta(seconds=args.alpha_collection_seconds)
            and (
                alpha_market_recorded_at is None
                or now - alpha_market_recorded_at >= timedelta(seconds=1)
            )
        ):
            as_of = now
            try:
                alpha_market = build_alpha_market_snapshot(
                    snapshot_id=f"alpha-market-{as_of.strftime('%Y%m%dT%H%M%S.%fZ')}",
                    as_of=as_of,
                    underlying=args.underlying,
                    contracts=contracts,
                    market_state=market_state,
                    feed_unsafe=source.health.data_unsafe,
                )
            except PricingRequestError:
                pass
            else:
                record_pricing_request(args.alpha_market_output, alpha_market)
                alpha_market_recorded_at = as_of
        if pricing_worker is not None:
            pricing_worker.request_update()
        count += 1
        if args.max_quotes > 0 and count >= args.max_quotes:
            source.stop()

    monitor = MarketMonitor(
        market_state,
        instruments,
        lambda: source.health,
        max_age=timedelta(seconds=5),
        json_path=args.snapshot or None,
        output=lambda message: print(message, flush=True),
    )

    print(
        f"loaded {len(contracts)} option contracts across "
        f"{len(summarize(contracts))} maturities",
        flush=True,
    )
    print(
        f"subscribing to {len(source.channels)} tick channels "
        f"({len(source.channels)-len(contracts)} underlying + {len(contracts)} options)",
        flush=True,
    )
    
    if pricing_worker is not None:
        pricing_worker.start()
    monitor.start()
    try:
        source.run(on_quote)
    except KeyboardInterrupt:
        source.stop()
    except Exception as exc:
        raise SystemExit(f"live feed failed: {type(exc).__name__}: {exc}") from exc
    finally:
        monitor.stop()
        if pricing_worker is not None:
            pricing_worker.stop()
            print(f"pricing health: {pricing_worker.health}", flush=True)
            latest_pricing = pricing_worker.latest()
            if latest_pricing is not None:
                print(
                    f"latest pricing: request={latest_pricing.request_id} "
                    f"options={len(latest_pricing.results)}",
                    flush=True,
                )
        if pricing_client is not None:
            pricing_client.close()
        if hedge_client is not None:
            hedge_client.close()
        print(f"feed health: {source.health}", flush=True)
        readiness = market_state.readiness(
            now=datetime.now(timezone.utc),
            max_age=timedelta(seconds=5),
            feed_unsafe=source.health.data_unsafe,
        )
        print(f"market state at shutdown: {readiness}", flush=True)
        print(f"latest quotes retained: {len(market_state.snapshot())}", flush=True)
        if args.record_pricing and not pricing_recorded:
            print(
                f"pricing request not recorded: {pricing_record_error}",
                flush=True,
            )


def load_option_chain(
    underlying: str,
    mode: str,
    parser: argparse.ArgumentParser,
) -> list[OptionContract]:
    token = os.getenv("DATA_TOKEN")
    if not token:
        parser.error("set DATA_TOKEN before requesting the option chain")

    with YmmReferenceDataSource(token=token, mode=mode) as source:
        contracts = source.load_option_chain(underlying, date.today())
    if not contracts:
        parser.error(f"no active option contracts found for {underlying}")
    return contracts


def quote_price(quote) -> float:
    """Choose the best available underlying price for universe selection."""

    if quote.last is not None:
        return quote.last
    if quote.bid is not None and quote.ask is not None:
        return (quote.bid + quote.ask) / 2
    if quote.bid is not None:
        return quote.bid
    if quote.ask is not None:
        return quote.ask
    raise ValueError(f"quote for {quote.instrument} contains no usable price")


def parse_clock_time(value: str):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must use HH:MM") from exc


def print_option_chain(underlying: str, contracts: list[OptionContract]) -> None:
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
