import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings
from app.services.data_ingestion_service import DataIngestionService
from app.services.dhan_service import DhanService


class TestDixonIngestionContract(unittest.TestCase):
    def test_resolve_symbol_security_map_is_dixon_only(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            underlying_symbol="DIXON",
            nifty_index_symbol="DIXON",
            nifty_symbols=("DIXON", "BANKNIFTY", "HDFCBANK", "ICICIBANK"),
            nifty50_security_map={
                "BANKNIFTY": "123",
                "HDFCBANK": "456",
                "ICICIBANK": "789",
            },
        )

        service = DataIngestionService(settings)
        symbol_map = service._resolve_symbol_security_map()

        self.assertEqual(set(symbol_map.keys()), {"DIXON"})
        self.assertEqual(symbol_map["DIXON"], settings.dhan.nifty_security_id)

    def test_intraday_provider_uses_two_day_history_window(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            intraday_fetch_days=2,
            dhan=DhanSettings(provider="groww"),
        )
        service = DhanService(settings)
        captured: dict[str, object] = {}

        def fake_fetch(**kwargs):
            captured.update(kwargs)
            return pd.DataFrame()

        service.get_groww_intraday_ohlc = fake_fetch
        now_market = datetime(2026, 9, 22, 9, 15, 0)

        service.get_ohlc("NIFTY 50", "15m", now_market=now_market)

        self.assertEqual(settings.intraday_fetch_days, 2)
        self.assertEqual(captured["from_date"], now_market - timedelta(days=2))
        self.assertEqual(captured["to_date"], now_market)


if __name__ == "__main__":
    unittest.main()
