from datetime import datetime, timezone
from dataclasses import replace
import unittest

from hedge_engine.config import HedgeConfig
from hedge_engine.msh import MinimalSufficientHedge
from hedge_engine.state import build_validated_state
from hedge_service import HedgeInstrument, HedgeRequest


NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)


class MinimalSufficientHedgeTests(unittest.TestCase):
    def test_flatten_proposal_is_stateless_and_repeatable(self) -> None:
        config = HedgeConfig(
            minimum_target_gross_reduction=1,
            minimum_slice_gross_reduction=1,
        )
        state = _state(bid_size=200, ask_size=200)
        policy = MinimalSufficientHedge(config, 100_000_000)

        first = policy.propose(state)
        second = policy.propose(state)

        self.assertEqual(first, {"CALL": -50})
        self.assertEqual(second, first)

    def test_executable_slice_is_capped_by_displayed_depth(self) -> None:
        config = HedgeConfig(
            depth_fraction=0.5,
            minimum_target_gross_reduction=1,
            minimum_slice_gross_reduction=1,
        )
        state = _state(bid_size=20, ask_size=20)

        result = MinimalSufficientHedge(config, 100_000_000).propose(state)

        self.assertEqual(result, {"CALL": -10})

    def test_sell_inventory_is_not_reduced_without_bid_depth(self) -> None:
        state = _state(bid_size=0, ask_size=200)
        config = HedgeConfig(
            minimum_target_gross_reduction=1,
            minimum_slice_gross_reduction=1,
        )

        result = MinimalSufficientHedge(config, 100_000_000).propose(state)

        self.assertEqual(result, {})

    def test_legacy_inventory_can_reduce_without_reaching_limit_in_one_slice(self) -> None:
        config = HedgeConfig(minimum_target_gross_reduction=1, minimum_slice_gross_reduction=1)
        for current in (700, -700):
            with self.subTest(current=current):
                state = _state(bid_size=100, ask_size=100)
                state = replace(state, context=replace(state.context,
                                hedge_positions={"CALL": current}, positions={"CALL": current}))
                policy = MinimalSufficientHedge(config, 200_000_000)
                orders = policy.propose(state)
                self.assertEqual(orders, {"CALL": -50 if current > 0 else 50})
                self.assertEqual(policy._evaluate_state(
                    state, {"CALL": 1 if current > 0 else -1}, target_map=None, require_depth=True,
                )["reason"], "position_limit")
                self.assertEqual(policy._evaluate_state(
                    state, {"CALL": -51 if current > 0 else 51}, target_map=None, require_depth=True,
                )["reason"], "depth")


def _state(*, bid_size: int, ask_size: int):
    config = HedgeConfig()
    request = HedgeRequest(
        request_id="hedge-msh",
        source_pricing_request_id="pricing-msh",
        market_as_of=NOW,
        account_id="ACCOUNT",
        base_ledger_revision=1,
        spot=100.0,
        instruments=(
            HedgeInstrument(
                instrument="CALL",
                option_type="CALL",
                strike=100.0,
                contract_multiplier=config.option_multiplier,
                delta=0.0,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                bid=1.0,
                ask=1.0,
                bid_size=bid_size,
                ask_size=ask_size,
            ),
        ),
        confirmed_alpha_positions={},
        confirmed_beta_positions={"CALL": 50},
        hedge_universe=("CALL",),
    )
    return build_validated_state(request, config, 100_000_000)


if __name__ == "__main__":
    unittest.main()
