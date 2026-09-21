"""Small fail-closed coordinator for automatic Alpha and Beta submission."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from time import sleep
from typing import Any, Mapping

from hedge_engine import empty_ledger, empty_order_registry
from sim_hedge.adapters.sim_trading import SimTradingError, SimTradingPortfolioSource
from sim_hedge.alpha_orders import build_alpha_order_dry_run
from sim_hedge.alpha_plan import build_plan_from_records, project_alpha_plan
from sim_hedge.alpha_reconcile import (
    load_all_fills,
    load_all_orders,
    reconcile_alpha_state,
    recover_order_bindings,
)
from sim_hedge.alpha_submit import submit_alpha_orders
from sim_hedge.beta_orders import build_beta_order_dry_run
from sim_hedge.beta_submit import submit_beta_orders
from sim_hedge.config import load_env_file
from sim_hedge.execution_monitor import build_accepted_execution_batch
from sim_hedge.order_registry import (
    registry_from_payload,
    registry_to_payload,
)
from sim_hedge.strategy_ledger import ledger_from_payload, ledger_to_payload


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
    parser.add_argument("--alpha-market", default="outputs/live-alpha-market.json")
    parser.add_argument("--risk", default="outputs/live_risk.json")
    parser.add_argument("--registry", default="outputs/order_registry.json")
    parser.add_argument("--ledger", default="outputs/strategy_ledger.json")
    parser.add_argument("--output-dir", default="outputs/auto")
    parser.add_argument("--budget-fraction", default="0.30")
    parser.add_argument("--max-alpha-contracts", required=True, type=int)
    parser.add_argument("--max-beta-contracts", required=True, type=int)
    parser.add_argument("--max-snapshot-age", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
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
    if args.max_alpha_contracts <= 0 or args.max_beta_contracts <= 0:
        parser.error("contract limits must be positive")

    source = _source(args.base_url, parser)
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
    last_status = None
    while True:
        try:
            status = run_once(
                source=source,
                account_id=args.enable_live_orders,
                exchange_id=args.exchange_id,
                alpha_market_path=Path(args.alpha_market),
                risk_path=Path(args.risk),
                registry_path=Path(args.registry),
                ledger_path=Path(args.ledger),
                output_dir=Path(args.output_dir),
                budget_fraction=Decimal(args.budget_fraction),
                max_alpha_contracts=args.max_alpha_contracts,
                max_beta_contracts=args.max_beta_contracts,
                max_snapshot_age=args.max_snapshot_age,
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

    if not registry.intents and not ledger.applied_trades:
        pricing = _object(_read(alpha_market_path), "Alpha market snapshot")
        if (
            portfolio.account.trading_day is None
            or pricing.get("tradingDate")
            != portfolio.account.trading_day.isoformat()
        ):
            raise ValueError("Alpha market and broker trading days do not match")
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
        _write(registry_path, registry_to_payload(registry))

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
    if not reconciliation.report["safe_for_hedging"]:
        return "WAITING: broker orders or fills are not fully reconciled"
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
    _write(registry_path, registry_to_payload(registry))
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


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} is not an object")
    return value


if __name__ == "__main__":
    main()
