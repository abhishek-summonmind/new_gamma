import os
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.api.routes import _refresh_service_for_symbol  # noqa: E402


class TestSymbolRefreshServiceRegistry(unittest.TestCase):
    def test_same_symbol_reuses_service_for_lock_and_debounce_state(self) -> None:
        first = _refresh_service_for_symbol("sensex")
        second = _refresh_service_for_symbol("SENSEX")

        self.assertIs(first, second)
        self.assertEqual(first._settings.underlying_symbol, "SENSEX")  # noqa: SLF001

    def test_each_index_has_its_own_scoped_service(self) -> None:
        sensex = _refresh_service_for_symbol("SENSEX")
        finnifty = _refresh_service_for_symbol("FINNIFTY")

        self.assertIsNot(sensex, finnifty)
        self.assertEqual(finnifty._settings.underlying_symbol, "FINNIFTY")  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
