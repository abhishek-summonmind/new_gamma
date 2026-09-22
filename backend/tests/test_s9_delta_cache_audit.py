import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.cache_service import CacheService  # noqa: E402
from app.services.dhan_client import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.option_metric_service import (  # noqa: E402
    OptionDeltaCacheProducer,
    option_metric_cache_key,
)
from app.services.refresh_service import RefreshService  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402


class TestS9DeltaCacheAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            database_url="sqlite:///./test.db",
            redis_url="memory://",
            underlying_symbol="DIXON",
            nifty_index_symbol="DIXON",
        )
        CacheService._GLOBAL_MEMORY_CACHE.clear()
        self.cache = CacheService(self.settings)

    @staticmethod
    def _chain(*contracts: OptionContract) -> OptionChainSnapshot:
        return OptionChainSnapshot(
            underlying=" dixon ",
            spot_price=15000.0,
            atm_strike=15000,
            expiry_date=date.today() + timedelta(days=7),
            snapshot_time=datetime.utcnow(),
            contracts=tuple(contracts),
        )

    @staticmethod
    def _contract(strike, option_type, *, delta=None, ltp=300.0) -> OptionContract:
        return OptionContract(
            security_id=f"{strike}-{option_type}",
            strike=strike,
            option_type=option_type,
            ltp=ltp,
            oi=1000.0,
            oi_change=10.0,
            volume=500.0,
            delta=delta,
            theta=-15.0,
        )

    def test_producer_and_consumer_use_identical_normalized_key(self) -> None:
        producer_key = option_metric_cache_key("DELTA", " dixon ", 15000.0, "ce", "5M")
        consumer_key = ScreenerEngine._option_metric_cache_key("delta", "DIXON 15000 CE", "5m")

        self.assertEqual(producer_key, "option:delta:DIXON 15000 CE:5m")
        self.assertEqual(producer_key, consumer_key)

    def test_provider_delta_present_for_ce_and_pe(self) -> None:
        chain = self._chain(
            self._contract(15000.0, "ce", delta=0.60),
            self._contract(15000, "PE", delta=-0.50),
        )
        summary = OptionDeltaCacheProducer(self.settings, self.cache).populate(chain)
        engine = ScreenerEngine(self.settings)

        ce = engine._check_delta_option("dixon 15000.0 ce", "CE")
        pe = engine._check_delta_option("DIXON 15000 PE", "PE")

        self.assertEqual(summary["provider"], 2)
        self.assertTrue(ce["passed"])
        self.assertTrue(pe["passed"])
        self.assertEqual(ce["data"]["delta"], 0.60)
        self.assertEqual(pe["data"]["delta"], -0.50)

    def test_missing_provider_delta_is_calculated_and_cached_before_s9(self) -> None:
        chain = self._chain(
            self._contract(15000.0, "CE", delta=None),
            self._contract(15000, "PE", delta=None),
        )
        summary = OptionDeltaCacheProducer(self.settings, self.cache).populate(chain)

        ce_payload = self.cache.get_json("option:delta:DIXON 15000 CE:5m")
        pe_payload = self.cache.get_json("option:delta:DIXON 15000 PE:5m")

        self.assertEqual(summary["calculated"], 2)
        self.assertIsInstance(ce_payload["delta"], float)
        self.assertIsInstance(pe_payload["delta"], float)
        self.assertEqual(ce_payload["source"], "black_scholes_estimate")
        self.assertIn("expiry", ce_payload)

    def test_unproducible_and_missing_delta_has_explicit_reason(self) -> None:
        chain = self._chain(self._contract(15000, "CE", delta=None, ltp=0))
        summary = OptionDeltaCacheProducer(self.settings, self.cache).populate(chain)
        check = ScreenerEngine(self.settings)._check_delta_option("DIXON 15000 CE", "CE")

        self.assertEqual(summary["missing"], 1)
        self.assertFalse(check["passed"])
        self.assertEqual(check["reason"], "delta_missing")
        self.assertEqual(check["data"]["lookup_key"], "option:delta:DIXON 15000 CE:5m")

    def test_repeated_override_refresh_claim_is_debounced(self) -> None:
        service = RefreshService(self.settings)
        key = "s9_override:default:bullish:none"

        self.assertTrue(service._claim_refresh_request(key, now=100.0))
        self.assertFalse(service._claim_refresh_request(key, now=100.5))
        self.assertTrue(service._claim_refresh_request(key, now=102.1))
        self.assertTrue(service._claim_refresh_request("s9_override:default:bearish:none", now=102.2))

        live_key = "s9_override:default:bullish:live"
        self.assertTrue(service._claim_refresh_request(live_key))
        response = service.run_refresh(
            trigger="s9_override",
            force=True,
            dedupe_key=live_key,
        )
        self.assertEqual(response["status"], "debounced")
        self.assertEqual(response["reason"], "duplicate_override_refresh")


if __name__ == "__main__":
    unittest.main()
