"""Translate hedging.v1 protobuf messages to and from the pure engine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from hedge_engine import HEDGE_PROPOSAL_VERSION
from hedge_service.engine import (
    ENGINE_NAME,
    ENGINE_VERSION,
    HedgeInstrument,
    HedgeRequest,
    HedgeResult,
)
from hedging.v1 import hedging_pb2


PROTOCOL_VERSION = "hedging.v1"


def request_from_proto(request: hedging_pb2.HedgeRequest) -> HedgeRequest:
    if request.configuration.model != hedging_pb2.HEDGE_MODEL_SCENARIO_ROUTED:
        raise ValueError("only the scenario-routed hedge model is supported")
    if not request.HasField("market_as_of"):
        raise ValueError("market_as_of is required")
    return HedgeRequest(
        request_id=request.request_id,
        source_pricing_request_id=request.source_pricing_request_id,
        market_as_of=request.market_as_of.ToDatetime(tzinfo=timezone.utc),
        account_id=request.account_id,
        base_ledger_revision=request.base_strategy_ledger_revision,
        spot=request.spot,
        instruments=tuple(
            HedgeInstrument(
                instrument=item.instrument,
                option_type=_option_type(item.option_type),
                strike=item.strike,
                contract_multiplier=item.contract_multiplier,
                delta=item.delta,
                gamma=item.gamma,
                theta=item.theta_per_year,
                vega=item.vega_per_absolute_volatility,
                bid=item.bid if item.HasField("bid") else None,
                ask=item.ask if item.HasField("ask") else None,
                bid_size=item.bid_size if item.HasField("bid_size") else None,
                ask_size=item.ask_size if item.HasField("ask_size") else None,
            )
            for item in request.instruments
        ),
        confirmed_alpha_positions=dict(request.confirmed_alpha_positions),
        confirmed_beta_positions=dict(request.confirmed_beta_positions),
        hedge_universe=tuple(request.hedge_universe),
    )


def response_to_proto(
    request_id: str, result: HedgeResult
) -> hedging_pb2.HedgeProposal:
    proposal = result.proposal
    message = hedging_pb2.HedgeProposal(
        request_id=request_id,
        protocol_version=str(proposal["protocol_version"]),
        proposal_id=str(proposal["proposal_id"]),
        proposal_type=str(proposal["proposal_type"]),
        source_pricing_request_id=str(proposal["source_pricing_request_id"]),
        account_id=str(proposal["account_id"]),
        base_strategy_ledger_revision=int(
            proposal["base_strategy_ledger_revision"]
        ),
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        confirmed_beta_positions=proposal["confirmed_beta_positions"],
        incremental_trades=proposal["incremental_trades"],
        target_beta_positions=proposal["target_beta_positions"],
        orders_generated=False,
        hedge_pair=result.hedge_pair,
        gamma_improvement=result.gamma_improvement,
        decision_policy=result.decision_policy,
    )
    created_at = datetime.fromisoformat(str(proposal["created_at"]))
    message.created_at.FromDatetime(created_at.astimezone(timezone.utc))
    message.source_market_as_of.FromDatetime(
        result.market_as_of.astimezone(timezone.utc)
    )
    _set_greeks(message.alpha_risk, result.alpha_risk)
    _set_greeks(message.confirmed_beta_risk, result.confirmed_beta_risk)
    _set_greeks(message.portfolio_risk, result.portfolio_risk)
    _set_greeks(message.risk_at_target_beta, result.risk_at_target_beta)
    diagnostics = result.execution_diagnostics
    message.execution_diagnostics.estimated_transaction_cost = (
        diagnostics.estimated_transaction_cost
    )
    message.execution_diagnostics.estimated_short_margin_before = (
        diagnostics.estimated_short_margin_before
    )
    message.execution_diagnostics.estimated_short_margin_after = (
        diagnostics.estimated_short_margin_after
    )
    message.execution_diagnostics.short_margin_limit = diagnostics.short_margin_limit
    for leg in diagnostics.legs:
        output = message.execution_diagnostics.legs.add(
            instrument=leg.instrument,
            side=leg.side,
            signed_quantity=leg.signed_quantity,
            estimated_transaction_cost=leg.estimated_transaction_cost,
            short_margin_per_contract=leg.short_margin_per_contract,
        )
        if leg.displayed_size is not None:
            output.displayed_size = leg.displayed_size
        if leg.displayed_depth_limit is not None:
            output.displayed_depth_limit = leg.displayed_depth_limit
    return message


def health_response() -> hedging_pb2.HealthResponse:
    return hedging_pb2.HealthResponse(
        engine_name=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        protocol_version=PROTOCOL_VERSION,
        proposal_protocol_version=HEDGE_PROPOSAL_VERSION,
    )


def _set_greeks(message, greeks) -> None:
    for name, value in asdict(greeks).items():
        setattr(message, name, value)


def _option_type(value: int) -> str:
    mapping = {
        hedging_pb2.OPTION_TYPE_CALL: "CALL",
        hedging_pb2.OPTION_TYPE_PUT: "PUT",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError("option type is unspecified or unsupported") from exc
