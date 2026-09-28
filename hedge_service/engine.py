"""Stateless request adapter and routing for the hedge service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from math import floor, isfinite
from numbers import Integral
from typing import Mapping

from hedge_engine import Greeks, InstrumentGreeks, aggregate_greeks, build_hedge_proposal
from hedge_engine.actions import scenario_risk
from hedge_engine.config import HedgeConfig
from hedge_engine.dg import delta_gamma_hedge
from hedge_engine.msh import MinimalSufficientHedge
from hedge_engine.state import build_validated_state


ENGINE_NAME = "reference-python-hedge"
ENGINE_VERSION = "0.4.0"


@dataclass(frozen=True)
class HedgeInstrument:
    instrument: str
    option_type: str
    strike: float
    contract_multiplier: int
    delta: float
    gamma: float
    theta: float
    vega: float
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if self.option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if (
            not isfinite(self.strike)
            or self.strike <= 0
            or isinstance(self.contract_multiplier, bool)
            or not isinstance(self.contract_multiplier, Integral)
            or self.contract_multiplier <= 0
        ):
            raise ValueError("strike and contract_multiplier must be positive")
        if not all(
            isfinite(value)
            for value in (self.delta, self.gamma, self.theta, self.vega)
        ):
            raise ValueError("instrument Greeks must be finite")
        if (self.bid is None) != (self.ask is None):
            raise ValueError("bid and ask must either both be present or both absent")
        if self.bid is not None and (
            not isfinite(self.bid) or not isfinite(self.ask)
            or self.bid <= 0 or self.ask < self.bid
        ):
            raise ValueError("bid and ask must be finite, positive, and non-crossed")
        for name in ("bid_size", "ask_size"):
            size = getattr(self, name)
            if size is not None and (
                isinstance(size, bool) or not isinstance(size, Integral) or size < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer when present")


@dataclass(frozen=True)
class HedgeRequest:
    request_id: str
    source_pricing_request_id: str
    market_as_of: datetime
    account_id: str
    base_ledger_revision: int
    spot: float
    instruments: tuple[HedgeInstrument, ...]
    confirmed_alpha_positions: Mapping[str, int]
    confirmed_beta_positions: Mapping[str, int]
    hedge_universe: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.request_id or not self.source_pricing_request_id:
            raise ValueError("request identifiers must not be empty")
        if not self.account_id:
            raise ValueError("account_id must not be empty")
        if self.market_as_of.tzinfo is None:
            raise ValueError("market_as_of must be timezone-aware")
        if isinstance(self.base_ledger_revision, bool) or self.base_ledger_revision < 0:
            raise ValueError("base_ledger_revision must not be negative")
        if not isfinite(self.spot) or self.spot <= 0:
            raise ValueError("spot must be positive")
        if not self.instruments:
            raise ValueError("at least one instrument is required")
        codes = [item.instrument for item in self.instruments]
        if len(codes) != len(set(codes)):
            raise ValueError("instrument inputs must be unique")
        if len(self.hedge_universe) != len(set(self.hedge_universe)):
            raise ValueError("hedge_universe must not contain duplicates")
        if set(self.hedge_universe) - set(codes):
            raise ValueError("hedge_universe contains an unknown instrument")


@dataclass(frozen=True)
class HedgeResult:
    proposal: Mapping[str, object]
    market_as_of: datetime
    hedge_pair: tuple[str, ...]
    alpha_risk: Greeks
    confirmed_beta_risk: Greeks
    portfolio_risk: Greeks
    risk_at_target_beta: Greeks
    gamma_improvement: float
    decision_policy: str
    execution_diagnostics: "HedgeExecutionDiagnostics"


@dataclass(frozen=True)
class HedgeExecutionLeg:
    instrument: str
    side: str
    signed_quantity: int
    displayed_size: int | None
    displayed_depth_limit: int | None
    estimated_transaction_cost: float
    short_margin_per_contract: float


@dataclass(frozen=True)
class HedgeExecutionDiagnostics:
    estimated_transaction_cost: float
    estimated_short_margin_before: float
    estimated_short_margin_after: float
    short_margin_limit: float
    legs: tuple[HedgeExecutionLeg, ...]


class ReferenceHedgeEngine:
    """Route breached scenario risk to D/G MILP, otherwise run stateless MSH."""

    name = ENGINE_NAME
    version = ENGINE_VERSION

    def __init__(
        self, *, config: HedgeConfig | None = None, capital: float = 100_000_000.0
    ):
        if not isfinite(capital) or capital <= 0:
            raise ValueError("capital must be finite and positive")
        self.config = config or HedgeConfig()
        self.capital = float(capital)

    def propose(self, request: HedgeRequest, *, created_at: datetime) -> HedgeResult:
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        multipliers = {item.contract_multiplier for item in request.instruments}
        if len(multipliers) != 1:
            raise ValueError("all option contract multipliers must be uniform")
        config = replace(self.config, option_multiplier=next(iter(multipliers)))
        state = build_validated_state(request, config, self.capital)
        if state.delta_breached or state.gamma_breached:
            decision_policy = "D_G_MILP"
            incremental = delta_gamma_hedge(state, config, self.capital)
        else:
            decision_policy = "MSH"
            incremental = MinimalSufficientHedge(config, self.capital).propose(state)

        target_beta = dict(state.context.hedge_positions)
        for code, quantity in incremental.items():
            target_beta[code] = target_beta.get(code, 0) + int(quantity)
            if not target_beta[code]:
                del target_beta[code]
        for code, alpha_quantity in state.context.strategy_positions.items():
            limit = config.alpha_hedge_ratio * abs(alpha_quantity)
            if abs(target_beta.get(code, 0)) > limit + 1e-9:
                raise ValueError(f"proposed Beta position for Alpha instrument {code} exceeds 30%")

        greeks_per_contract = {
            item.instrument: InstrumentGreeks(
                item.instrument,
                Greeks(
                    delta=item.delta * config.option_multiplier,
                    gamma=item.gamma * config.option_multiplier,
                    vega=item.vega * config.option_multiplier,
                    theta=item.theta * config.option_multiplier,
                ),
            )
            for item in request.instruments
        }
        target_beta_risk = aggregate_greeks(target_beta, greeks_per_contract)
        risk_at_target = state.strategy_greeks.plus(target_beta_risk)
        risk_after = scenario_risk(
            risk_at_target, state.spot,
            config.risk_spot_shock_fraction, config.risk_vol_shock,
        )
        gamma_improvement = abs(state.gamma_risk) - abs(float(risk_after[1]))
        execution_diagnostics = _execution_diagnostics(
            state=state,
            incremental=incremental,
            target_beta=target_beta,
            config=config,
            capital=self.capital,
            decision_policy=decision_policy,
        )
        proposal = build_hedge_proposal(
            pricing_request_id=request.source_pricing_request_id,
            account_id=request.account_id,
            base_ledger_revision=request.base_ledger_revision,
            created_at=created_at,
            engine_name=self.name,
            engine_version=self.version,
            confirmed_beta_positions=state.context.hedge_positions,
            incremental_trades=incremental,
        )
        return HedgeResult(
            proposal=proposal,
            market_as_of=request.market_as_of,
            hedge_pair=tuple(sorted(incremental)),
            alpha_risk=state.strategy_greeks,
            confirmed_beta_risk=state.hedge_greeks,
            portfolio_risk=state.portfolio_greeks,
            risk_at_target_beta=risk_at_target,
            gamma_improvement=gamma_improvement,
            decision_policy=decision_policy,
            execution_diagnostics=execution_diagnostics,
        )


def _execution_diagnostics(
    *, state, incremental, target_beta, config, capital, decision_policy
) -> HedgeExecutionDiagnostics:
    cost = 0.0
    legs = []
    for code, signed_quantity in sorted(incremental.items()):
        instrument = state.instruments[code]
        if instrument.bid is None or instrument.ask is None or instrument.mid is None:
            raise ValueError(f"live quote missing for proposed hedge instrument {code}")
        cost_per_contract = (
            max(instrument.ask - instrument.mid, instrument.mid - instrument.bid)
            * config.option_multiplier
            + config.option_fee
        )
        cost += abs(int(signed_quantity)) * cost_per_contract
        displayed_size = (
            instrument.ask_size if signed_quantity > 0 else instrument.bid_size
        )
        legs.append(
            HedgeExecutionLeg(
                instrument=code,
                side="BUY" if signed_quantity > 0 else "SELL",
                signed_quantity=int(signed_quantity),
                displayed_size=displayed_size,
                displayed_depth_limit=(
                    None
                    if displayed_size is None
                    else (
                        floor(config.depth_fraction * displayed_size)
                        if decision_policy == "MSH"
                        else displayed_size
                    )
                ),
                estimated_transaction_cost=(
                    abs(int(signed_quantity)) * cost_per_contract
                ),
                short_margin_per_contract=float(state.margins[code]),
            )
        )

    def short_margin(beta_positions):
        positions = dict(state.context.strategy_positions)
        for code, quantity in beta_positions.items():
            positions[code] = positions.get(code, 0) + int(quantity)
        return float(
            sum(
                -int(quantity) * state.margins[code]
                for code, quantity in positions.items()
                if quantity < 0
            )
        )

    return HedgeExecutionDiagnostics(
        estimated_transaction_cost=float(cost),
        estimated_short_margin_before=short_margin(state.context.hedge_positions),
        estimated_short_margin_after=short_margin(target_beta),
        short_margin_limit=float(capital * config.margin_limit_fraction),
        legs=tuple(legs),
    )
