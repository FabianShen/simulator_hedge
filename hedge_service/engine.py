"""Pure orchestration for one stateless hedge proposal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

from hedge_engine import (
    ALPHA_HEDGE_RATIO,
    Greeks,
    InstrumentGreeks,
    build_hedge_proposal,
    evaluate_delta_gamma_hedge,
    integerize_delta_gamma_hedge,
    aggregate_greeks,
)


ENGINE_NAME = "reference-python-hedge"
ENGINE_VERSION = "0.3.0"


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


@dataclass(frozen=True)
class HedgeResult:
    proposal: Mapping[str, object]
    market_as_of: datetime
    hedge_pair: tuple[str, ...]
    alpha_risk: Greeks
    confirmed_beta_risk: Greeks
    portfolio_risk: Greeks
    risk_at_target_beta: Greeks
    normalized_residual: float


class ReferenceHedgeEngine:
    """Risk-band monitored, bounded multi-instrument Delta/Gamma policy."""

    name = ENGINE_NAME
    version = ENGINE_VERSION

    def __init__(
        self,
        *,
        delta_limit: float,
        gamma_limit: float,
        target_delta: float = 0.0,
        target_gamma: float = 0.0,
        alpha_hedge_ratio: float = ALPHA_HEDGE_RATIO,
        alpha_modification_penalty: float = 2.0,
    ):
        if not isfinite(delta_limit) or delta_limit < 0:
            raise ValueError("delta_limit must be finite and non-negative")
        if not isfinite(gamma_limit) or gamma_limit < 0:
            raise ValueError("gamma_limit must be finite and non-negative")
        if not isfinite(target_delta) or not isfinite(target_gamma):
            raise ValueError("Delta/Gamma targets must be finite")
        if (
            not isfinite(alpha_hedge_ratio)
            or not 0 <= alpha_hedge_ratio <= ALPHA_HEDGE_RATIO
        ):
            raise ValueError("alpha_hedge_ratio must be between zero and 30%")
        if (
            not isfinite(alpha_modification_penalty)
            or alpha_modification_penalty < 0
        ):
            raise ValueError("alpha_modification_penalty must not be negative")
        self.delta_limit = delta_limit
        self.gamma_limit = gamma_limit
        self.target_delta = target_delta
        self.target_gamma = target_gamma
        self.alpha_hedge_ratio = alpha_hedge_ratio
        self.alpha_modification_penalty = alpha_modification_penalty

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
        for code in (
            set(request.confirmed_alpha_positions)
            & set(request.confirmed_beta_positions)
        ):
            limit = self.alpha_hedge_ratio * abs(
                request.confirmed_alpha_positions[code]
            )
            if abs(request.confirmed_beta_positions[code]) > limit:
                raise ValueError(
                    f"Beta position for Alpha instrument {code} exceeds "
                    f"{self.alpha_hedge_ratio:.0%} of frozen Alpha"
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

        alpha_risk = aggregate_greeks(request.confirmed_alpha_positions, greeks)
        beta_risk = aggregate_greeks(request.confirmed_beta_positions, greeks)
        portfolio_risk = alpha_risk.plus(beta_risk)

        delta_breached = (
            abs(portfolio_risk.delta - self.target_delta) > self.delta_limit
        )
        gamma_breached = (
            abs(portfolio_risk.gamma - self.target_gamma) > self.gamma_limit
        )

        if not delta_breached and not gamma_breached:
            # Risk is within the hedge bands.
            # MSH will be added here later.
            proposal = build_hedge_proposal(
                        pricing_request_id=request.source_pricing_request_id,
                        account_id=request.account_id,
                        base_ledger_revision=request.base_ledger_revision,
                        created_at=created_at,
                        engine_name=self.name,
                        engine_version=self.version,
                        confirmed_beta_positions=request.confirmed_beta_positions,
                        incremental_trades={},
                    )
            return HedgeResult(
                proposal=proposal,
                market_as_of=request.market_as_of,
                hedge_pair=(),
                alpha_risk=alpha_risk,
                confirmed_beta_risk=beta_risk,
                portfolio_risk=portfolio_risk,
                risk_at_target_beta=portfolio_risk,
                normalized_residual=0.0,
            )

        decision = evaluate_delta_gamma_hedge(
            alpha_positions=request.confirmed_alpha_positions,
            hedge_positions=request.confirmed_beta_positions,
            instrument_greeks=greeks,
            hedge_universe=request.hedge_universe,
            alpha_hedge_ratio=self.alpha_hedge_ratio,
            alpha_modification_penalty=self.alpha_modification_penalty,
            target_delta=self.target_delta,
            target_gamma=self.target_gamma,
        )
        tradable = integerize_delta_gamma_hedge(
            decision,
            instrument_greeks=greeks,
            hedge_universe=request.hedge_universe,
            alpha_positions=request.confirmed_alpha_positions,
            hedge_positions=request.confirmed_beta_positions,
            alpha_hedge_ratio=self.alpha_hedge_ratio,
            target_delta=self.target_delta,
            target_gamma=self.target_gamma,
        )
        active_instruments = tuple(
            code
            for code in request.hedge_universe
            if tradable.integer_incremental_trades[code]
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
            hedge_pair=active_instruments,
            alpha_risk=decision.alpha_risk,
            confirmed_beta_risk=decision.current_hedge_risk,
            portfolio_risk=decision.before_hedge,
            risk_at_target_beta=tradable.after_integer_hedge,
            normalized_residual=tradable.normalized_residual,
        )
