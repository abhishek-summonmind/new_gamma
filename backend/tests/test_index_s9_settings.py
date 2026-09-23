import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings, normalize_market_symbol  # noqa: E402
from app.services.cache_service import CacheService  # noqa: E402
from app.services.dhan_client import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.refresh_service import RefreshService  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402


class TestIndexS9Settings(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            database_url="sqlite:///./test.db",
            redis_url="memory://",
            underlying_symbol="FINNIFTY",
            nifty_index_symbol="FINNIFTY",
        )
        CacheService._GLOBAL_MEMORY_CACHE.clear()
        self.engine = ScreenerEngine(self.settings)

    @staticmethod
    def _contract(delta: float | None = 0.60) -> OptionContract:
        return OptionContract(
            security_id="idx-ce",
            strike=23500.0,
            option_type="CE",
            ltp=120.0,
            oi=1000.0,
            oi_change=1.0,
            volume=500.0,
            delta=delta,
            theta=-30.0,
            bid_qty=70.0,
            ask_qty=30.0,
        )

    def test_supported_index_symbol_normalization(self) -> None:
        self.assertEqual(normalize_market_symbol("nifty50"), "NIFTY 50")
        self.assertEqual(normalize_market_symbol("fin nifty"), "FINNIFTY")
        self.assertEqual(normalize_market_symbol("sensex"), "SENSEX")

    def test_index_delta_band_and_ignored_theta(self) -> None:
        self.assertTrue(self.engine._check_delta_option("FINNIFTY 23500 CE", "CE", self._contract(0.65))["passed"])
        self.assertTrue(self.engine._check_delta_option("FINNIFTY 23500 PE", "PE", self._contract(-0.55))["passed"])
        self.assertFalse(self.engine._check_delta_option("FINNIFTY 23500 CE", "CE", self._contract(0.66))["passed"])
        theta = self.engine._check_theta_option("FINNIFTY 23500 CE", self._contract())
        self.assertTrue(theta["passed"])
        self.assertTrue(theta["data"]["ignored"])

    def test_index_pcr_ignored_but_pcr_shift_uses_new_threshold(self) -> None:
        chain = OptionChainSnapshot(
            underlying="FINNIFTY",
            spot_price=23500.0,
            atm_strike=23500,
            expiry_date=date.today(),
            snapshot_time=datetime.utcnow(),
            contracts=(
                self._contract(),
                OptionContract("idx-pe", 23500.0, "PE", 110.0, 1000.0, 1.0, 500.0),
            ),
        )
        cache = CacheService(self.settings)
        cache.set_json("pcr:history", [{"pcr": 0.96}])
        pcr = self.engine._check_pcr_buy(chain)
        call_shift = self.engine._check_pcr_shift_buy(chain, None)  # type: ignore[arg-type]

        self.assertTrue(pcr["passed"])
        self.assertTrue(pcr["data"]["ignored"])
        self.assertTrue(call_shift["passed"])
        self.assertAlmostEqual(call_shift["data"]["pcr_shift"], 0.04)
        self.assertEqual(call_shift["data"]["timeframe"], "3m")

        cache.set_json("pcr:history", [{"pcr": 1.04}])
        put_shift = self.engine._check_pcr_shift_sell(chain, None)  # type: ignore[arg-type]
        self.assertTrue(put_shift["passed"])
        self.assertAlmostEqual(put_shift["data"]["pcr_shift"], -0.04)
        self.assertEqual(put_shift["data"]["timeframe"], "3m")

    def test_screener_cache_is_symbol_specific(self) -> None:
        self.assertEqual(RefreshService(self.settings)._screener_cache_key("S9"), "dashboard:screener:s9:FINNIFTY")


if __name__ == "__main__":
    unittest.main()
