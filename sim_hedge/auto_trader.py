"""Small fail-closed coordinator for automatic Alpha and Beta submission."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from time import sleep
from typing import Any, Mapping, Sequence

from hedge_engine import (
    OrderRegistry,
    StrategyLedger,
    abandon_unsubmitted_intents,
    empty_ledger,
    empty_order_registry,
    retire_verified_cancelled_beta,
)
from sim_hedge.adapters.sim_trading import (
    BrokerOrder,
    SimTradingError,
    SimTradingPortfolioSource,
    SimTradingUnknownOutcomeError,
)
from sim_hedge.jsonio import (
    read_json as _read,
    require_object as _object,
    write_json as _write,
)
from sim_hedge.execution.alpha.orders import build_alpha_order_dry_run
from sim_hedge.execution.alpha.continuation import (
    assess_alpha_continuation,
    select_alpha_continuation_request,
)
from sim_hedge.execution.alpha.finalize import validate_alpha_adoption
from sim_hedge.execution.alpha.plan import build_plan_from_records, project_alpha_plan
from sim_hedge.execution.alpha.reconcile import (
    load_all_fills,
    load_all_orders,
    reconcile_alpha_state,
    recover_order_bindings,
)
from sim_hedge.execution.alpha.submit import submit_alpha_orders
from sim_hedge.execution.beta.orders import build_beta_order_dry_run
from sim_hedge.execution.beta.submit import submit_beta_orders
from sim_hedge.config import load_env_file
from sim_hedge.state.monitor import build_accepted_execution_batch
from sim_hedge.state.registry import (
    registry_from_payload,
    registry_to_payload,
)
from sim_hedge.execution.submission import (
    execute_registered_requests,
    validate_registered_requests,
    validate_timestamp_freshness,
)
from sim_hedge.state.ledger import ledger_from_payload, ledger_to_payload


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Automatically initialize Alpha, reconcile, and submit fresh Beta hedges"
    )
    parser.add_argument(
        "--enable-live-orders",
        required=True,
        metavar="ACCOUNT_ID",
        help="one startup account binding; subsequent valid orders are automatic",
    )
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--exchange-id", default="SZSE")
    parser.add_argument("--alpha-market")
    parser.add_argument("--risk")
    parser.add_argument("--registry", default="outputs/order_registry.json")
    parser.add_argument("--ledger", default="outputs/strategy_ledger.json")
    parser.add_argument("--output-dir", default="outputs/auto")
    parser.add_argument("--budget-fraction", default="0.30")
    parser.add_argument("--max-alpha-contracts", required=True, type=int)
    parser.add_argument("--max-beta-contracts", required=True, type=int)
    parser.add_argument("--max-snapshot-age", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--cancel-after",
        type=float,
        default=180.0,
        help="cancel a stuck working Beta order older than this many seconds",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--recover-unsubmitted-alpha",
        action="store_true",
        help="broker-verified, registry-only recovery; never submits orders",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="verify authentication and account identity without submitting",
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.cancel_after <= 0:
        parser.error("--cancel-after must be positive")
    if args.max_alpha_contracts <= 0 or args.max_beta_contracts <= 0:
        parser.error("contract limits must be positive")
    if args.preflight and args.recover_unsubmitted_alpha:
        parser.error("--preflight and --recover-unsubmitted-alpha are exclusive")

    source = _source(args.base_url, parser)
    ledger_path = Path(args.ledger)
    default_ledger = Path("outputs/strategy_ledger.json")
    alpha_market_path = Path(args.alpha_market) if args.alpha_market else (
        Path("outputs/live-alpha-market.json")
        if ledger_path == default_ledger else ledger_path.with_name("alpha_market.json")
    )
    risk_path = Path(args.risk) if args.risk else (
        Path("outputs/live_risk.json")
        if ledger_path == default_ledger else ledger_path.with_name("live_risk.json")
    )
    if args.preflight:
        portfolio = source.load(args.enable_live_orders)
        print(
            f"preflight account={portfolio.account.account_id} "
            f"status={portfolio.account.status} "
            f"risk={portfolio.account.risk_state or '?'} "
            f"positions={len(portfolio.positions)} "
            f"active_orders={len(portfolio.active_orders)}; orders submitted=0"
        )
        return
    if args.recover_unsubmitted_alpha:
        try:
            count = recover_unsubmitted_alpha(
                source=source,
                account_id=args.enable_live_orders,
                registry_path=Path(args.registry),
                ledger_path=ledger_path,
            )
        except (ValueError, KeyError, json.JSONDecodeError, OSError, SimTradingError) as exc:
            raise SystemExit(f"Alpha recovery blocked: {exc}") from exc
        print(f"RECOVERED: abandoned {count} unsubmitted Alpha intents; orders submitted=0")
        return
    print(f"inputs: alpha_market={alpha_market_path} risk={risk_path}", flush=True)
    last_status = None
    while True:
        try:
            status = run_once(
                source=source,
                account_id=args.enable_live_orders,
                exchange_id=args.exchange_id,
                alpha_market_path=alpha_market_path,
                risk_path=risk_path,
                registry_path=Path(args.registry),
                ledger_path=ledger_path,
                output_dir=Path(args.output_dir),
                budget_fraction=Decimal(args.budget_fraction),
                max_alpha_contracts=args.max_alpha_contracts,
                max_beta_contracts=args.max_beta_contracts,
                max_snapshot_age=args.max_snapshot_age,
                cancel_after=args.cancel_after,
            )
        except SimTradingUnknownOutcomeError as exc:
            status = (
                "STOP BETA: cancellation outcome is unknown; "
                f"reconcile before restart: {exc}"
            )
        except (ValueError, KeyError, json.JSONDecodeError, OSError, SimTradingError) as exc:
            status = f"BLOCKED: {type(exc).__name__}: {exc}"
        if status != last_status:
            print(status, flush=True)
            last_status = status
        if args.once or status.startswith("STOP "):
            return
        sleep(args.interval)


def run_once(
    *,
    source: SimTradingPortfolioSource,
    account_id: str,
    exchange_id: str,
    alpha_market_path: Path,
    risk_path: Path,
    registry_path: Path,
    ledger_path: Path,
    output_dir: Path,
    budget_fraction: Decimal,
    max_alpha_contracts: int,
    max_beta_contracts: int,
    max_snapshot_age: float,
    cancel_after: float = 180.0,
) -> str:
    """Perform at most one submission batch, always after reading broker state."""

    portfolio = source.load(account_id)
    if portfolio.account.account_id != account_id:
        raise ValueError("broker returned a different account")

    if registry_path.exists():
        registry = registry_from_payload(_object(_read(registry_path), "registry"))
        if registry.account_id != account_id:
            raise ValueError("order registry belongs to another account")
    else:
        registry = empty_order_registry(account_id)
    if ledger_path.exists():
        ledger = ledger_from_payload(_object(_read(ledger_path), "ledger"))
        if ledger.account_id != account_id:
            raise ValueError("strategy ledger belongs to another account")
    else:
        ledger = empty_ledger(account_id)

    adoption_path = output_dir / "alpha_adoption.json"
    adopted = adoption_path.exists()
    if adopted:
        plan_path = output_dir / "alpha_plan.json"
        validate_alpha_adoption(
            _object(_read(adoption_path), "Alpha adoption"),
            _object(_read(plan_path), "saved Alpha plan"),
            registry, ledger,
        )

    active_intents = set(registry.intents) - set(registry.abandoned_client_order_ids)
    if (
        not active_intents
        and not registry.broker_orders
        and not registry.unknown_client_order_ids
        and not ledger.applied_trades
        and not ledger.alpha_positions
        and not ledger.beta_positions
    ):
        pricing = _object(_read(alpha_market_path), "Alpha market snapshot")
        if (
            portfolio.account.trading_day is None
            or pricing.get("tradingDate")
            != portfolio.account.trading_day.isoformat()
        ):
            raise ValueError("Alpha market and broker trading days do not match")
        validate_timestamp_freshness(
            "Alpha market snapshot", pricing.get("asOf"),
            now=datetime.now(timezone.utc), max_age=max_snapshot_age,
        )
        portfolio_record = asdict(portfolio)
        portfolio_record["positions"] = list(portfolio_record["positions"])
        portfolio_record["active_orders"] = list(
            portfolio_record["active_orders"]
        )
        plan = build_plan_from_records(
            pricing,
            portfolio_record,
            budget_fraction=budget_fraction,
            max_contracts_per_option=None,
        )
        alpha = project_alpha_plan(plan, pricing, account_id=account_id)
        dry_run, registry = build_alpha_order_dry_run(
            alpha, exchange_id=exchange_id, registry=registry
        )
        total = sum(int(request["volume"]) for request in dry_run["requests"])
        if total > max_alpha_contracts:
            raise ValueError(
                f"margin-sized Alpha volume {total} exceeds --max-alpha-contracts "
                f"{max_alpha_contracts}"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        _write(output_dir / "alpha_plan.json", alpha)
        _write(output_dir / "alpha_orders.json", dry_run)
        def persist_alpha(value) -> None:
            _write(registry_path, registry_to_payload(value))

        registry, report = submit_alpha_orders(
            proposal=dry_run,
            registry=registry,
            portfolio=portfolio,
            submit=source.submit_etf_option_order,
            persist=persist_alpha,
            now=datetime.now(timezone.utc),
            max_total_contracts=max_alpha_contracts,
            max_snapshot_age_seconds=max_snapshot_age,
        )
        _write(registry_path, registry_to_payload(registry))
        _write(output_dir / "alpha_submission.json", report)
        return _submission_status("ALPHA", report)

    trading_day = portfolio.account.trading_day
    if trading_day is None:
        raise ValueError("broker snapshot has no trading_day")
    orders = load_all_orders(source, account_id, trading_day)
    recovered = recover_order_bindings(registry, orders)
    fills = load_all_fills(source, recovered)
    portfolio = source.load(account_id)
    reconciliation = reconcile_alpha_state(
        registry=recovered,
        ledger=ledger,
        fills=fills,
        orders=orders,
        portfolio=portfolio,
        previous_registry=registry,
    )
    registry = reconciliation.registry
    ledger = reconciliation.ledger
    _write(registry_path, registry_to_payload(registry))
    _write(ledger_path, ledger_to_payload(ledger))
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "reconciliation.json", reconciliation.report)
    recovered_beta = _recover_stuck_beta(
        source=source,
        registry=registry,
        ledger=ledger,
        orders=orders,
        cancel_after=cancel_after,
        now=datetime.now(timezone.utc),
        registry_path=registry_path,
    )
    if recovered_beta is not None:
        return recovered_beta
    if not reconciliation.report["safe_for_hedging"]:
        plan_path = output_dir / "alpha_plan.json"
        if plan_path.exists() and ledger.alpha_positions and not adopted:
            assessment = assess_alpha_continuation(
                _object(_read(plan_path), "saved Alpha plan"),
                registry,
                ledger,
                reconciliation.report,
            )
            _write(output_dir / "alpha_continuation.json", assessment)
            if assessment["status"] in {"WAITING_FOR_BROKER", "NEEDS_TOP_UP"}:
                if any(order.order_id not in registry.broker_orders for order in orders):
                    return "WAITING ALPHA: broker has an order outside the registry"
                if any(order.status not in {"ACCEPTED", "PARTIALLY_FILLED", "FILLED"} for order in orders):
                    return "WAITING ALPHA: a broker order was cancelled or has an unknown status"
                if sum(abs(value) for value in assessment["target_positions"].values()) > max_alpha_contracts:
                    raise ValueError("saved Alpha target exceeds --max-alpha-contracts")
                market = _object(_read(alpha_market_path), "Alpha market snapshot")
                if market.get("tradingDate") != trading_day.isoformat():
                    raise ValueError("Alpha market and broker trading days do not match")
                retry_path = output_dir / "alpha_retry_after.json"
                retry_after = _retry_after(_read(retry_path)) if retry_path.exists() else {}
                now = datetime.now(timezone.utc)
                request, reason = select_alpha_continuation_request(
                    assessment, market, registry, now=now,
                    max_age=max_snapshot_age, retry_after=retry_after,
                )
                if request is None:
                    return f"WAITING ALPHA: {reason}"
                validate_registered_requests(
                    [request], registry, strategy="ALPHA",
                    max_total_contracts=max_alpha_contracts,
                )
                current_portfolio = source.load(account_id)
                active_order_ids = {order.order_id for order in current_portfolio.active_orders}
                if (
                    current_portfolio.account.account_id != account_id
                    or current_portfolio.account.trading_day != trading_day
                    or current_portfolio.account.account_type != "ETF_OPTION"
                    or current_portfolio.signed_positions != ledger.alpha_positions
                    or not active_order_ids <= set(registry.broker_orders)
                    or any(
                        order.instrument == str(request["symbol"])
                        for order in current_portfolio.active_orders
                    )
                    or current_portfolio.account.status != "NORMAL"
                    or current_portfolio.account.risk_state not in (None, "NORMAL")
                ):
                    raise ValueError("broker state changed before Alpha continuation")

                def persist_alpha_continuation(value) -> None:
                    _write(registry_path, registry_to_payload(value))

                persist_alpha_continuation(registry)
                registry, outcomes = execute_registered_requests(
                    registry, (request,),
                    submit=source.submit_etf_option_order,
                    persist=persist_alpha_continuation,
                )
                _write(output_dir / "alpha_continuation_submission.json", {
                    "attempted_at": now.isoformat(),
                    "account_id": account_id,
                    "source_alpha_market_id": market.get("requestId"),
                    **outcomes,
                })
                if outcomes["unknown"]:
                    return "STOP ALPHA: submission outcome is unknown; reconcile before restart"
                if outcomes["rejected"]:
                    error = outcomes["rejected"][0]["error"]
                    if "BID1_MISSING" not in error:
                        return f"STOP ALPHA: broker rejected continuation: {error}"
                    retry_after[str(request["client_order_id"])] = now + timedelta(seconds=30)
                    _write(retry_path, {key: value.isoformat() for key, value in retry_after.items()})
                    return "WAITING ALPHA: broker bid missing; other eligible legs may continue"
                return "SUBMITTED ALPHA: 1 continuation order"
        return "WAITING: broker orders or fills are not fully reconciled"
    plan_path = output_dir / "alpha_plan.json"
    if plan_path.exists() and ledger.alpha_positions and not adopted:
        assessment = assess_alpha_continuation(
            _object(_read(plan_path), "saved Alpha plan"),
            registry,
            ledger,
            reconciliation.report,
        )
        _write(output_dir / "alpha_continuation.json", assessment)
        if assessment["status"] != "TARGET_REACHED":
            return f"WAITING ALPHA: {assessment['status']}"
    if not risk_path.exists():
        return "WAITING: no live hedge proposal"

    risk = _object(_read(risk_path), "live hedge proposal")
    if risk.get("status") != "READY":
        return "WAITING: live hedge proposal is not ready"
    accepted = build_accepted_execution_batch(risk, ledger)
    already_seen = {
        intent.proposal_id
        for intent in registry.intents.values()
        if intent.proposal_id is not None
    }
    if accepted.get("proposal_id") in already_seen:
        return "WAITING: latest hedge proposal was already submitted"
    dry_run, registry = build_beta_order_dry_run(
        accepted,
        ledger,
        registry,
        exchange_id=exchange_id,
        max_total_contracts=max_beta_contracts,
        replace_unsubmitted=False,
    )
    _write(output_dir / "accepted_hedge_proposal.json", accepted)
    _write(output_dir / "beta_orders.json", dry_run)
    if not dry_run["requests"]:
        return "READY: hedge proposal requires no Beta orders"

    def persist_beta(value) -> None:
        _write(registry_path, registry_to_payload(value))

    registry, report = submit_beta_orders(
        proposal=dry_run,
        registry=registry,
        ledger=ledger,
        portfolio=portfolio,
        submit=source.submit_etf_option_order,
        persist=persist_beta,
        now=datetime.now(timezone.utc),
        max_total_contracts=max_beta_contracts,
        max_snapshot_age_seconds=max_snapshot_age,
    )
    _write(registry_path, registry_to_payload(registry))
    _write(output_dir / "beta_submission.json", report)
    return _submission_status("BETA", report)


def _recover_stuck_beta(
    *,
    source: SimTradingPortfolioSource,
    registry: OrderRegistry,
    ledger: StrategyLedger,
    orders: Sequence[BrokerOrder],
    cancel_after: float,
    now: datetime,
    registry_path: Path,
) -> str | None:
    """Retire cancelled Beta orders or cancel stale orders, including partial fills."""

    if cancel_after <= 0:
        raise ValueError("cancel_after must be positive")
    if registry.account_id != ledger.account_id:
        raise ValueError("order registry and strategy ledger accounts do not match")

    retired: list[str] = []
    updated = registry
    for order in orders:
        client_id = updated.broker_orders.get(order.order_id)
        intent = updated.intents.get(client_id) if client_id is not None else None
        if (
            intent is None
            or intent.strategy != "BETA"
            or order.status.upper() not in {"CANCELLED", "PARTIALLY_CANCELLED"}
            or order.remaining_volume != 0
            or client_id in updated.retired_cancelled_client_order_ids
        ):
            continue
        updated = retire_verified_cancelled_beta(updated, client_id)
        retired.append(order.order_id)
    if retired:
        _write(registry_path, registry_to_payload(updated))
        return f"RETIRED cancelled Beta order(s): {', '.join(sorted(retired))}"

    stuck: list[str] = []
    for order in orders:
        client_id = registry.broker_orders.get(order.order_id)
        intent = registry.intents.get(client_id) if client_id is not None else None
        status = order.status.upper()
        if (
            intent is None
            or intent.strategy != "BETA"
            or status not in {"ACCEPTED", "PARTIALLY_FILLED"}
            or order.remaining_volume == 0
            or (now - order.created_at).total_seconds() <= cancel_after
        ):
            continue
        source.cancel_etf_option_order(order.order_id, ledger.account_id)
        stuck.append(order.order_id)
    if stuck:
        return f"CANCELLED stuck Beta order(s): {', '.join(sorted(stuck))}"
    return None


def recover_unsubmitted_alpha(
    *,
    source: SimTradingPortfolioSource,
    account_id: str,
    registry_path: Path,
    ledger_path: Path,
) -> int:
    """Retire orphaned Alpha intents only after checking the empty broker account.

    This deliberately cannot submit or cancel an order. Any ambiguous broker
    evidence blocks recovery rather than silently discarding ownership.
    """

    registry = registry_from_payload(_object(_read(registry_path), "registry"))
    ledger = ledger_from_payload(_object(_read(ledger_path), "ledger"))
    if registry.account_id != account_id or ledger.account_id != account_id:
        raise ValueError("registry, ledger, and requested account IDs do not match")
    if (
        registry.broker_orders or registry.unknown_client_order_ids
        or registry.superseded_client_order_ids
        or ledger.applied_trades or ledger.alpha_positions or ledger.beta_positions
    ):
        raise ValueError("submitted/unknown orders or confirmed trades exist")
    pending = tuple(sorted(
        client_id for client_id in registry.intents
        if client_id not in registry.abandoned_client_order_ids
    ))
    if not pending or any(registry.intents[client_id].strategy != "ALPHA" for client_id in pending):
        raise ValueError("no exclusively unsubmitted Alpha intents to recover")
    portfolio = source.load(account_id)
    if portfolio.account.account_id != account_id or portfolio.account.trading_day is None:
        raise ValueError("broker account or trading day could not be verified")
    if portfolio.account.status != "NORMAL" or portfolio.account.risk_state not in (None, "NORMAL"):
        raise ValueError("broker account is not healthy")
    if portfolio.positions or portfolio.active_orders:
        raise ValueError("broker has positions or active orders")
    if any(registry.intents[client_id].created_at.date() != portfolio.account.trading_day for client_id in pending):
        raise ValueError("intent creation day differs from broker trading day")
    if load_all_orders(source, account_id, portfolio.account.trading_day):
        raise ValueError("broker order history is not empty")
    if load_all_fills(source, registry):
        raise ValueError("broker has confirmed trades")
    # Recheck after paginated reads to catch changes while inspecting history.
    portfolio = source.load(account_id)
    if portfolio.account.account_id != account_id or portfolio.positions or portfolio.active_orders:
        raise ValueError("broker state changed during recovery")
    updated = abandon_unsubmitted_intents(registry, pending)
    _write(registry_path, registry_to_payload(updated))
    return len(pending)


def _submission_status(strategy: str, report: Mapping[str, Any]) -> str:
    if report["unknown"]:
        return f"STOP {strategy}: submission outcome is unknown; reconcile before restart"
    if report["rejected"]:
        return f"BLOCKED {strategy}: broker rejected an order"
    return f"SUBMITTED {strategy}: {len(report['accepted'])} orders"


def _source(base_url: str, parser: argparse.ArgumentParser) -> SimTradingPortfolioSource:
    token = os.getenv("SIM_ACCESS_TOKEN")
    source = SimTradingPortfolioSource(base_url, access_token=token)
    if token:
        return source
    username = os.getenv("SIM_USERNAME")
    password = os.getenv("SIM_PASSWORD")
    if not username or not password:
        parser.error("set SIM_ACCESS_TOKEN, or set SIM_USERNAME and SIM_PASSWORD")
    source.login(username, password)
    return source


def _retry_after(value: Any) -> dict[str, datetime]:
    raw = _object(value, "Alpha retry schedule")
    schedule = {}
    for client_id, timestamp in raw.items():
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Alpha retry timestamp must be timezone-aware")
        schedule[str(client_id)] = parsed
    return schedule


if __name__ == "__main__":
    main()
