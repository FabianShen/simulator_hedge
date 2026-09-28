"""Build and record an Alpha plan from saved pricing and portfolio snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from typing import Any, Mapping

from sim_hedge.execution.alpha.strategy import AlphaPlan, build_short_otm_alpha_plan
from sim_hedge.domain.contract import OptionContract, OptionType
from sim_hedge.jsonio import read_json as _read_json, require_object as _object, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an offline Alpha plan")
    parser.add_argument("pricing_request")
    parser.add_argument("portfolio_snapshot")
    parser.add_argument("--output", default="outputs/alpha_plan.json")
    parser.add_argument("--budget-fraction", default="0.30")
    parser.add_argument("--max-contracts-per-option", type=int, default=1)
    args = parser.parse_args()

    try:
        pricing = _object(_read_json(args.pricing_request), "pricing request")
        portfolio = _object(_read_json(args.portfolio_snapshot), "portfolio snapshot")
        account = _object(portfolio.get("account"), "portfolio account")
        plan = build_plan_from_records(
            pricing,
            portfolio,
            budget_fraction=Decimal(args.budget_fraction),
            max_contracts_per_option=args.max_contracts_per_option,
        )
        projection = project_alpha_plan(
            plan,
            pricing,
            account_id=str(account.get("account_id") or ""),
        )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"alpha plan failed: {exc}") from exc
    write_json(args.output, projection, ensure_ascii=False)
    print(
        f"alpha plan: maturity={plan.maturity.isoformat()} "
        f"options={len(plan.legs)} margin_capacity={plan.margin_capacity} "
        f"contracts_per_option={plan.contracts_per_option} "
        f"budget={plan.margin_budget}"
    )
    print("sizing basis: exchange short-option opening margin")
    print(f"wrote Alpha plan: {args.output}")


def build_plan_from_records(
    pricing: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    *,
    budget_fraction: Decimal = Decimal("0.30"),
    max_contracts_per_option: int | None = 1,
) -> AlphaPlan:
    positions = portfolio.get("positions")
    active_orders = portfolio.get("active_orders")
    if not isinstance(positions, list) or not isinstance(active_orders, list):
        raise ValueError("portfolio positions and active_orders must be lists")
    if positions:
        raise ValueError("Alpha initialization requires an empty broker portfolio")
    if active_orders:
        raise ValueError("Alpha initialization requires no active broker orders")

    underlying = _object(pricing.get("underlying"), "pricing underlying")
    underlying_id = str(underlying.get("instrument") or "")
    spot = underlying.get("spot")
    if not underlying_id or spot is None:
        raise ValueError("pricing underlying is missing instrument or spot")
    options = pricing.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("pricing request has no options")

    contracts: list[OptionContract] = []
    previous_settlements: dict[str, Any] = {}
    for value in options:
        option = _object(value, "pricing option")
        instrument = str(option.get("instrument") or "")
        expiry = str(option.get("expiry") or "")
        option_type = _option_type(option.get("optionType"))
        if not instrument or not expiry:
            raise ValueError("pricing option is missing instrument or expiry")
        contracts.append(
            OptionContract(
                instrument=instrument,
                underlying=underlying_id,
                option_type=option_type,
                strike=float(option["strike"]),
                maturity=_datetime(expiry).date(),
                contract_multiplier=int(option["contractMultiplier"]),
                price_tick=float(option["priceTick"]),
            )
        )
        previous_settlements[instrument] = option.get("previousSettlement")

    account = _object(portfolio.get("account"), "portfolio account")
    initial_cash = account.get("cash_balance")
    if initial_cash is None:
        raise ValueError("portfolio account has no cash_balance")
    return build_short_otm_alpha_plan(
        contracts,
        spot=float(spot),
        previous_underlying_close=underlying.get("previousClose"),
        previous_settlements=previous_settlements,
        initial_cash=initial_cash,
        budget_fraction=budget_fraction,
        max_contracts_per_option=max_contracts_per_option,
    )


def project_alpha_plan(
    plan: AlphaPlan,
    pricing: Mapping[str, Any],
    *,
    account_id: str,
) -> dict[str, Any]:
    prices = {
        str(option["instrument"]): Decimal(str(option["marketPrice"]))
        for option in pricing["options"]
    }
    return {
        "plan_id": f"alpha-{pricing.get('requestId', 'unknown')}",
        "source_alpha_market_id": pricing.get("requestId"),
        "account_id": account_id,
        "as_of": pricing.get("asOf"),
        "underlying": plan.underlying,
        "maturity": plan.maturity.isoformat(),
        "initial_cash": str(plan.initial_cash),
        "budget_fraction": str(plan.budget_fraction),
        "margin_budget": str(plan.margin_budget),
        "one_contract_basket_margin": str(plan.one_contract_basket_margin),
        "margin_capacity": plan.margin_capacity,
        "max_contracts_per_option": plan.max_contracts_per_option,
        "contracts_per_option": plan.contracts_per_option,
        "sizing_basis": "SHORT_OPTION_OPENING_MARGIN",
        "orders_generated": False,
        "legs": [
            {
                "instrument": leg.contract.instrument,
                "option_type": leg.contract.option_type.value,
                "strike": leg.contract.strike,
                "reference_price": str(prices[leg.contract.instrument]),
                "previous_settlement": str(
                    next(
                        option["previousSettlement"]
                        for option in pricing["options"]
                        if option["instrument"] == leg.contract.instrument
                    )
                ),
                "contract_multiplier": leg.contract.contract_multiplier,
                "quantity": leg.quantity,
            }
            for leg in plan.legs
        ],
    }


def _option_type(value: Any) -> OptionType:
    if value == "OPTION_TYPE_CALL":
        return OptionType.CALL
    if value == "OPTION_TYPE_PUT":
        return OptionType.PUT
    raise ValueError(f"unknown optionType: {value!r}")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    main()
