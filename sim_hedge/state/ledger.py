"""Replay normalized broker-confirmed fills into the strategy ledger."""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, DecimalException
import json
import os
from pathlib import Path
from typing import Any, Mapping

from hedge_engine import (
    ConfirmedFill,
    StrategyLedger,
    apply_confirmed_fills,
    empty_ledger,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay confirmed broker fills")
    parser.add_argument("confirmed_fills")
    parser.add_argument("--ledger", help="existing strategy ledger")
    parser.add_argument("--output", default="outputs/strategy_ledger.json")
    args = parser.parse_args()
    try:
        payload = _object(_read(args.confirmed_fills), "confirmed fills")
        if payload.get("source") != "BROKER_CONFIRMED":
            raise ValueError("fill source must be BROKER_CONFIRMED")
        account_id = str(payload.get("account_id") or "")
        ledger = (
            ledger_from_payload(_object(_read(args.ledger), "strategy ledger"))
            if args.ledger
            else empty_ledger(account_id)
        )
        fills = fills_from_payload(payload)
        updated = apply_confirmed_fills(ledger, fills)
        _write(args.output, ledger_to_payload(updated))
    except (ValueError, KeyError, DecimalException, json.JSONDecodeError) as exc:
        raise SystemExit(f"strategy ledger failed: {exc}") from exc
    print(
        f"confirmed ledger revision={updated.revision} "
        f"alpha_positions={len(updated.alpha_positions)} "
        f"beta_positions={len(updated.beta_positions)}"
    )
    print(f"wrote confirmed strategy ledger: {args.output}")


def fills_from_payload(payload: Mapping[str, Any]) -> tuple[ConfirmedFill, ...]:
    raw_fills = payload.get("fills")
    if not isinstance(raw_fills, list):
        raise ValueError("fills must be a list")
    account_id = str(payload.get("account_id") or "")
    result = []
    for value in raw_fills:
        raw = _object(value, "fill")
        result.append(
            ConfirmedFill(
                trade_id=str(raw.get("trade_id") or ""),
                order_id=str(raw.get("order_id") or ""),
                account_id=account_id,
                strategy=str(raw.get("strategy") or "").upper(),
                instrument=str(raw.get("instrument") or ""),
                quantity=_integer(raw.get("quantity"), "fill quantity"),
                price=Decimal(str(raw.get("price"))),
                executed_at=_datetime(str(raw.get("executed_at") or "")),
            )
        )
    return tuple(result)


def ledger_to_payload(ledger: StrategyLedger) -> dict[str, Any]:
    return {
        "status": "CONFIRMED",
        "account_id": ledger.account_id,
        "revision": ledger.revision,
        "strategies": {
            "ALPHA": {"actual_positions": dict(ledger.alpha_positions)},
            "BETA": {"actual_positions": dict(ledger.beta_positions)},
        },
        "applied_trades": {
            trade_id: _fill_payload(fill)
            for trade_id, fill in sorted(ledger.applied_trades.items())
        },
    }


def ledger_from_payload(payload: Mapping[str, Any]) -> StrategyLedger:
    if payload.get("status") != "CONFIRMED":
        raise ValueError("existing strategy ledger must be CONFIRMED")
    strategies = _object(payload.get("strategies"), "ledger strategies")
    trades = _object(payload.get("applied_trades"), "applied trades")
    parsed_trades: dict[str, ConfirmedFill] = {}
    for trade_id, raw in trades.items():
        fill_payload = {
            **_object(raw, "applied trade"),
            "trade_id": trade_id,
        }
        fill = fills_from_payload(
            {
                "account_id": payload.get("account_id"),
                "fills": [fill_payload],
            }
        )[0]
        parsed_trades[str(trade_id)] = fill
    return StrategyLedger(
        account_id=str(payload.get("account_id") or ""),
        revision=_integer(payload.get("revision"), "ledger revision"),
        alpha_positions=_positions(strategies, "ALPHA"),
        beta_positions=_positions(strategies, "BETA"),
        applied_trades=parsed_trades,
    )


def _positions(strategies: Mapping[str, Any], name: str) -> dict[str, int]:
    strategy = _object(strategies.get(name), f"{name} strategy")
    positions = _object(strategy.get("actual_positions"), f"{name} positions")
    result = {}
    for code, quantity in positions.items():
        parsed = _integer(quantity, f"{name} position {code}")
        if parsed:
            result[str(code)] = parsed
    return result


def _fill_payload(fill: ConfirmedFill) -> dict[str, Any]:
    return {
        "order_id": fill.order_id,
        "strategy": fill.strategy,
        "instrument": fill.instrument,
        "quantity": fill.quantity,
        "price": str(fill.price),
        "executed_at": fill.executed_at.isoformat(),
    }


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} is not an object")
    return value


def _integer(value: Any, name: str) -> int:
    number = Decimal(str(value))
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("fill executed_at must be timezone-aware")
    return result


if __name__ == "__main__":
    main()
