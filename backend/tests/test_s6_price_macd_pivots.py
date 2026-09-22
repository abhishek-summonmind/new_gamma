import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.cache_service import CacheService  # noqa: E402
from app.services.price_macd_divergence_service import PriceMacdDivergenceService  # noqa: E402


class TestS6Pivots(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_url="sqlite:///./test.db", redis_url="memory://")

    def test_pivot_flags_strict(self) -> None:
        series = pd.Series([1, 2, 3, 4, 5])
        is_hh, is_ll = PriceMacdDivergenceService._pivot_flags(series, lookback=3)  # noqa: SLF001
        self.assertTrue(is_hh)
        self.assertFalse(is_ll)

        series2 = pd.Series([5, 4, 3, 2, 1])
        is_hh, is_ll = PriceMacdDivergenceService._pivot_flags(series2, lookback=3)  # noqa: SLF001
        self.assertFalse(is_hh)
        self.assertTrue(is_ll)

        # Equality should not count as HH/LL.
        series3 = pd.Series([1, 2, 3, 3])
        is_hh, is_ll = PriceMacdDivergenceService._pivot_flags(series3, lookback=3)  # noqa: SLF001
        self.assertFalse(is_hh)
        self.assertFalse(is_ll)

    def test_signal_time_holds_when_action_same(self) -> None:
        cache = CacheService(self.settings)
        service = PriceMacdDivergenceService(self.settings, cache=cache)

        # Force tiny universe for determinism.
        service._settings.nifty_symbols = ("RELIANCE",)  # type: ignore[attr-defined]  # noqa: SLF001

        t1 = datetime(2026, 5, 1, 9, 20, 0)
        row1, _ = service._finalize_row(  # noqa: SLF001
            symbol="RELIANCE",
            timeframe="5m",
            close=101.0,
            prev_close=100.0,
            macd=1.0,
            prev_macd=1.2,
            action="fake_buy",
            candle_time=t1,
            price_hh=True,
            price_ll=False,
            macd_hh=False,
            macd_ll=False,
        )
        self.assertEqual(row1.signal_time, t1.isoformat())

        t2 = datetime(2026, 5, 1, 9, 25, 0)
        row2, _ = service._finalize_row(  # noqa: SLF001
            symbol="RELIANCE",
            timeframe="5m",
            close=102.0,
            prev_close=101.0,
            macd=0.9,
            prev_macd=1.0,
            action="fake_buy",
            candle_time=t2,
            price_hh=True,
            price_ll=False,
            macd_hh=False,
            macd_ll=False,
        )
        self.assertEqual(row2.signal_time, t1.isoformat())

        t3 = datetime(2026, 5, 1, 9, 30, 0)
        row3, _ = service._finalize_row(  # noqa: SLF001
            symbol="RELIANCE",
            timeframe="5m",
            close=103.0,
            prev_close=102.0,
            macd=1.1,
            prev_macd=0.9,
            action="watch",
            candle_time=t3,
            price_hh=False,
            price_ll=False,
            macd_hh=False,
            macd_ll=False,
        )
        self.assertEqual(row3.signal_time, t3.isoformat())


if __name__ == "__main__":
    unittest.main()

