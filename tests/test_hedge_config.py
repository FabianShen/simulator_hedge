import unittest

from hedge_engine.config import HedgeConfig


class HedgeConfigTargetTests(unittest.TestCase):
    def test_raw_centers_and_limits_convert_to_scenario_risk(self) -> None:
        config = HedgeConfig(
            target_delta=5_000.0,
            delta_limit=1_000.0,
            target_gamma=-200.0,
            gamma_limit=300.0,
        )

        resolved = config.resolved_for(20.0)

        self.assertAlmostEqual(resolved.delta_center_risk, 1_000.0)
        self.assertAlmostEqual(resolved.delta_entry_risk_band, 200.0)
        self.assertAlmostEqual(resolved.delta_target_risk_band, 200.0)
        self.assertAlmostEqual(resolved.gamma_center_risk, -4.0)
        self.assertAlmostEqual(resolved.gamma_entry_risk_band, 6.0)
        self.assertAlmostEqual(resolved.gamma_target_risk_band, 6.0)

    def test_default_resolution_preserves_neutral_scenario_bands(self) -> None:
        config = HedgeConfig()

        self.assertIs(config.resolved_for(3.3), config)

    def test_nonzero_target_requires_a_raw_greek_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "target_delta requires delta_limit"):
            HedgeConfig(target_delta=5_000.0)
        with self.assertRaisesRegex(ValueError, "target_gamma requires gamma_limit"):
            HedgeConfig(target_gamma=1.0)

    def test_raw_limits_must_be_positive_and_allow_a_zero_center(self) -> None:
        with self.assertRaisesRegex(ValueError, "delta_limit must be finite and positive"):
            HedgeConfig(delta_limit=0.0)

        resolved = HedgeConfig(delta_limit=1_000.0).resolved_for(100.0)
        self.assertEqual(resolved.delta_center_risk, 0.0)
        self.assertEqual(
            resolved.delta_entry_risk_band, resolved.delta_target_risk_band
        )


if __name__ == "__main__":
    unittest.main()
