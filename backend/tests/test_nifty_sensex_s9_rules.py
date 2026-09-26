import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402


class TestRemovedLegacyIndexS9Rules(unittest.TestCase):
    def test_nifty_and_sensex_are_outside_s9_entry_scope(self) -> None:
        for symbol in ("NIFTY 50", "SENSEX"):
            engine = ScreenerEngine(
                Settings(
                    database_url="sqlite:///./test.db",
                    redis_url="memory://",
                    underlying_symbol=symbol,
                    nifty_index_symbol=symbol,
                )
            )
            result = engine._run_s9(
                states_by_timeframe={},
                s1_signals=[],
                option_chain=None,
                now_market=datetime(2026, 9, 25, 10, 0),
            )
            self.assertEqual(result, [])

    def test_default_s9_indices_are_banknifty_and_finnifty_only(self) -> None:
        settings = Settings(database_url="sqlite:///./test.db", redis_url="memory://")
        self.assertEqual(settings.s9_scan_symbols, ("BANK NIFTY", "FINNIFTY"))


if __name__ == "__main__":
    unittest.main()
