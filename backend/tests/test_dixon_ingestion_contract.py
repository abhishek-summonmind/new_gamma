import os
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings
from app.services.data_ingestion_service import DataIngestionService


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


if __name__ == "__main__":
    unittest.main()
