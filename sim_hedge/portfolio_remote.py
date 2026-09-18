"""Inspect normalized simulated-trading state without placing orders."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from sim_hedge.adapters.sim_trading import SimTradingPortfolioSource


def main() -> None:
    parser = argparse.ArgumentParser(description="Read simulated portfolio state")
    parser.add_argument("--base-url", default=os.getenv("SIM_REST_BASE_URL", ""))
    parser.add_argument("--account-id", default=os.getenv("SIM_ACCOUNT_ID", ""))
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="list accessible accounts instead of loading a portfolio",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="optionally write normalized diagnostics as JSON",
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error("set SIM_REST_BASE_URL or pass --base-url")
    if not args.list_accounts and not args.account_id:
        parser.error("set SIM_ACCOUNT_ID or pass --account-id")

    source = SimTradingPortfolioSource(
        args.base_url,
        access_token=os.getenv("SIM_ACCESS_TOKEN"),
    )
    if not os.getenv("SIM_ACCESS_TOKEN"):
        username = os.getenv("SIM_USERNAME")
        password = os.getenv("SIM_PASSWORD")
        if not username or not password:
            parser.error(
                "set SIM_ACCESS_TOKEN, or set both SIM_USERNAME and SIM_PASSWORD"
            )
        source.login(username, password)

    if args.list_accounts:
        accounts = source.list_accounts()
        for account in accounts:
            account_id = account.get("account_id") or account.get("id") or "?"
            account_type = account.get("account_type") or account.get("type") or "?"
            status = account.get("status") or "?"
            print(f"{account_id} type={account_type} status={status}")
        return

    snapshot = source.load(args.account_id)
    print(
        f"portfolio account={snapshot.account.account_id} "
        f"type={snapshot.account.account_type or '?'} "
        f"status={snapshot.account.status or '?'} "
        f"risk={snapshot.account.risk_state or '?'} "
        f"positions={len(snapshot.positions)} "
        f"active_orders={len(snapshot.active_orders)}"
    )
    for position in snapshot.positions:
        print(
            f"position {position.instrument} {position.direction} "
            f"volume={position.volume} available={position.available_volume}"
        )
    if args.output:
        _write_json(args.output, asdict(snapshot))
        print(f"wrote normalized portfolio: {args.output}")


def _write_json(path: str, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, default=_json_default, indent=2, ensure_ascii=False)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False
    ) as handle:
        handle.write(payload)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return, union-attr]
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    main()
