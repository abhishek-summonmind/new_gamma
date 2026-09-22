import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.data_ingestion_service import DataIngestionService  # noqa: E402


class TestMockOptionExpiry(unittest.TestCase):
    def test_mock_chain_uses_runtime_configured_expiry(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            redis_url="memory://",
            market_data_mode="mock",
            underlying_symbol="SENSEX",
            nifty_index_symbol="SENSEX",
            nifty_symbols=("SENSEX",),
        )
        service = DataIngestionService(settings)
        configured_expiry = date(2026, 8, 30)
        now_market = datetime(2026, 7, 28, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        with (
            patch.object(service, "_replace_candles"),
            patch(
                "app.services.data_ingestion_service.DhanConfigService.get_effective_option_expiry",
                return_value=configured_expiry,
            ),
        ):
            result = service._ingest_mock(db=None, run_id=1, now_market=now_market)  # type: ignore[arg-type]

        self.assertIsNotNone(result.option_chain)
        self.assertEqual(result.option_chain.expiry_date, configured_expiry)
        self.assertIn(configured_expiry.isoformat(), result.warnings[0])

    def test_mock_chain_falls_back_to_market_date_without_configuration(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            redis_url="memory://",
            market_data_mode="mock",
            underlying_symbol="SENSEX",
            nifty_index_symbol="SENSEX",
            nifty_symbols=("SENSEX",),
        )
        settings.dhan.option_expiry = None
        service = DataIngestionService(settings)
        now_market = datetime(2026, 7, 28, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        with (
            patch.object(service, "_replace_candles"),
            patch(
                "app.services.data_ingestion_service.DhanConfigService.get_effective_option_expiry",
                return_value=None,
            ),
        ):
            result = service._ingest_mock(db=None, run_id=1, now_market=now_market)  # type: ignore[arg-type]

        self.assertIsNotNone(result.option_chain)
        self.assertEqual(result.option_chain.expiry_date, now_market.date())


if __name__ == "__main__":
    unittest.main()

