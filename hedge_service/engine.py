"""Pure orchestration for one stateless hedge proposal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

from hedge_engine import (
    Greeks,
    InstrumentGreeks,
    build_hedge_proposal,
    evaluate_delta_gamma_hedge,
    integerize_delta_gamma_hedge,
)


ENGINE_NAME = "reference-python-hedge"
ENGINE_VERSION = "0.1.0"


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

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must not be empty")
        if self.option_type not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        if (
            not isfinite(self.strike)
            or self.strike <= 0
            or self.contract_multiplier <= 0
        ):
            raise ValueError("strike and contract_multiplier must be positive")
        if not all(
            isfinite(value)
            for value in (self.delta, self.gamma, self.theta, self.vega)
        ):
            raise ValueError("instrument Greeks must be finite")


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
        if self.base_ledger_revision < 0:
            raise ValueError("base_ledger_revision must not be negative")
        if not isfinite(self.spot) or self.spot <= 0:
            raise ValueError("spot must be positive")
        if not self.instruments:
            raise ValueError("at least one instrument is required")
        codes = [item.instrument for item in self.instruments]
        if len(codes) != len(set(codes)):
            raise ValueError("instrument inputs must be unique")
        if set(self.confirmed_alpha_positions) & set(self.confirmed_beta_positions):
            raise ValueError("Alpha and Beta positions must not overlap")


@dataclass(frozen=True)
class HedgeResult:
    proposal: Mapping[str, object]
    market_as_of: datetime
    hedge_pair: tuple[str, str]
    alpha_risk: Greeks
    confirmed_beta_risk: Greeks
    portfolio_risk: Greeks
    risk_at_target_beta: Greeks
    normalized_residual: float


class ReferenceHedgeEngine:
    """Current simple two-instrument Delta/Gamma implementation."""

    name = ENGINE_NAME
    version = ENGINE_VERSION

    def propose(self, request: HedgeRequest, *, created_at: datetime) -> HedgeResult:
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        metadata = {item.instrument: item for item in request.instruments}
        held = set(request.confirmed_alpha_positions) | set(
            request.confirmed_beta_positions
        )
        missing = held - set(metadata)
        if missing:
            raise ValueError(
                "held positions are missing instrument Greeks: "
                + ", ".join(sorted(missing))
            )
        unknown_candidates = set(request.hedge_universe) - set(metadata)
        if unknown_candidates:
            raise ValueError(
                "hedge universe is missing instrument Greeks: "
                + ", ".join(sorted(unknown_candidates))
            )

        greeks = {
            code: InstrumentGreeks(
                code,
                Greeks(
                    delta=item.delta * item.contract_multiplier,
                    gamma=item.gamma * item.contract_multiplier,
                    theta=item.theta * item.contract_multiplier,
                    vega=item.vega * item.contract_multiplier,
                ),
            )
            for code, item in metadata.items()
        }
        pair = _select_pair(
            metadata,
            allowed=set(request.hedge_universe)
            - set(request.confirmed_alpha_positions),
            spot=request.spot,
            greeks=greeks,
        )
        decision = evaluate_delta_gamma_hedge(
            alpha_positions=request.confirmed_alpha_positions,
            hedge_positions=request.confirmed_beta_positions,
            instrument_greeks=greeks,
            hedge_pair=pair,
        )
        tradable = integerize_delta_gamma_hedge(
            decision,
            instrument_greeks=greeks,
            hedge_pair=pair,
        )
        proposal = build_hedge_proposal(
            pricing_request_id=request.source_pricing_request_id,
            account_id=request.account_id,
            base_ledger_revision=request.base_ledger_revision,
            created_at=created_at,
            engine_name=self.name,
            engine_version=self.version,
            confirmed_beta_positions=request.confirmed_beta_positions,
            incremental_trades=tradable.integer_incremental_trades,
        )
        return HedgeResult(
            proposal=proposal,
            market_as_of=request.market_as_of,
            hedge_pair=pair,
            alpha_risk=decision.alpha_risk,
            confirmed_beta_risk=decision.current_hedge_risk,
            portfolio_risk=decision.before_hedge,
            risk_at_target_beta=tradable.after_integer_hedge,
            normalized_residual=tradable.normalized_residual,
        )


def _select_pair(
    metadata: Mapping[str, HedgeInstrument],
    *,
    allowed: set[str],
    spot: float,
    greeks: Mapping[str, InstrumentGreeks],
) -> tuple[str, str]:
    calls = sorted(
        (
            item
            for code, item in metadata.items()
            if code in allowed and item.option_type == "CALL"
        ),
        key=lambda item: abs(item.strike - spot),
    )
    puts = sorted(
        (
            item
            for code, item in metadata.items()
            if code in allowed and item.option_type == "PUT"
        ),
        key=lambda item: abs(item.strike - spot),
    )
    pairs = sorted(
        ((call, put) for call in calls for put in puts),
        key=lambda pair: abs(pair[0].strike - spot) + abs(pair[1].strike - spot),
    )
    for call, put in pairs:
        first = greeks[call.instrument].greeks_per_contract
        second = greeks[put.instrument].greeks_per_contract
        determinant = first.delta * second.gamma - second.delta * first.gamma
        if abs(determinant) >= 1e-12:
            return call.instrument, put.instrument
    raise ValueError("no non-singular call/put pair is available for hedging")
