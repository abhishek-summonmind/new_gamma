import unittest

from app.services.macro_risk_service import MacroRiskService


class TestMacroRiskService(unittest.TestCase):
    def effects(self, symbol="INFY", direction="bullish", context=None):
        return MacroRiskService.effects_for(
            symbol=symbol,
            direction=direction,
            base_score=96,
            context=context,
            brent_config={"risk_threshold_pct": 1.0},
            usd_config={},
            confluence_bonus=4,
        )

    def test_green_daily_vix_confirms_existing_direction_only(self):
        bullish = self.effects(context={"india_vix": {"change_pct": 1.2}})
        bearish = self.effects(direction="bearish", context={"india_vix": {"change_pct": 1.2}})
        self.assertTrue(bullish["india_vix_confirmed"])
        self.assertTrue(bearish["india_vix_confirmed"])
        self.assertFalse(bullish["vix_risk_filter_active"])

    def test_red_daily_vix_activates_risk_without_confirmation(self):
        effects = self.effects(context={"india_vix": {"change_pct": -0.2}})
        self.assertFalse(effects["india_vix_confirmed"])
        self.assertTrue(effects["vix_risk_filter_active"])
        self.assertEqual(effects["macro_confirmation_score"], 0)

    def test_sector_and_flow_rules_are_directional_and_capped(self):
        context = {
            "india_vix": {"change_pct": 1},
            "brent": {"change_pct": 1.5},
            "usd_inr": {"change_pct": 0.4},
            "fii_dii": {"fii_net": -438.2, "dii_net": 100},
        }
        put = self.effects(symbol="MARUTI", direction="bearish", context=context)
        self.assertTrue(put["brent_confirmed"])
        self.assertTrue(put["fii_dii_confirmed"])
        self.assertLessEqual(put["macro_confirmation_score"], 4)
        call = self.effects(symbol="MARUTI", direction="bullish", context=context)
        self.assertTrue(call["crude_risk"])
        self.assertFalse(call["brent_confirmed"])

    def test_missing_context_is_neutral(self):
        effects = self.effects(context=None)
        self.assertEqual(effects["macro_confirmation_score"], 0)
        self.assertFalse(effects["crude_risk"])


if __name__ == "__main__":
    unittest.main()
