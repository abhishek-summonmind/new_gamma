import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.utils.timeframe import resample_ohlcv  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402


class TestS9CompletedTrendCandles(unittest.TestCase):
    @staticmethod
    def _frame() -> pd.DataFrame:
        market_tz = ZoneInfo("Asia/Kolkata")
        sessions = [
            pd.date_range(
                start=datetime(2026, 9, day, 9, 15, tzinfo=market_tz),
                end=datetime(2026, 9, day, 15, 29, tzinfo=market_tz),
                freq="1min",
            )
            for day in (21, 22)
        ]
        index = sessions[0].append(sessions[1])
        values = range(len(index))
        return pd.DataFrame(
            {"open": values, "high": values, "low": values, "close": values, "volume": 1.0},
            index=index,
        )

    def _completed(self, timeframe: str, hour: int, minute: int) -> pd.DataFrame:
        return resample_ohlcv(
            self._frame(),
            timeframe=timeframe,
            timezone_name="Asia/Kolkata",
            market_open_time="09:15",
            market_close_time="15:30",
            now_market=datetime(2026, 9, 22, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata")),
            completed_only=True,
        )

    def test_30m_uses_previous_session_at_0920(self) -> None:
        candles = self._completed("30m", 9, 20)
        self.assertEqual(candles.index[-1], pd.Timestamp("2026-09-21 15:15", tz="Asia/Kolkata"))

    def test_30m_uses_completed_session_candle_at_0934(self) -> None:
        candles = self._completed("30m", 9, 34)
        self.assertEqual(candles.index[-1], pd.Timestamp("2026-09-21 15:15", tz="Asia/Kolkata"))

    def test_30m_does_not_use_running_candle_at_1014(self) -> None:
        candles = self._completed("30m", 10, 14)
        self.assertEqual(candles.index[-1], pd.Timestamp("2026-09-22 09:45", tz="Asia/Kolkata"))
        self.assertNotIn(pd.Timestamp("2026-09-22 10:30", tz="Asia/Kolkata"), candles.index)

    def test_30m_uses_first_session_candle_after_completion(self) -> None:
        candles = self._completed("30m", 9, 45)
        self.assertEqual(candles.index[-1], pd.Timestamp("2026-09-22 09:45", tz="Asia/Kolkata"))
        self.assertEqual(candles.iloc[-1]["close"], self._frame().loc["2026-09-22 09:44"]["close"])

    def test_15m_uses_only_latest_completed_candle(self) -> None:
        self.assertEqual(
            self._completed("15m", 9, 20).index[-1],
            pd.Timestamp("2026-09-21 15:30", tz="Asia/Kolkata"),
        )
        self.assertEqual(
            self._completed("15m", 9, 34).index[-1],
            pd.Timestamp("2026-09-22 09:30", tz="Asia/Kolkata"),
        )

    def test_s9_direction_prefers_completed_trend_states(self) -> None:
        candle_time = datetime(2026, 9, 22, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))

        def state(close: float, ema9: float) -> SimpleNamespace:
            return SimpleNamespace(latest=SimpleNamespace(candle_time=candle_time, close=close, ema9=ema9))

        states = {
            "30m": {"NIFTY 50": state(101.0, 100.0)},
            "15m": {"NIFTY 50": state(101.0, 100.0)},
            "s9_30m_completed": {"NIFTY 50": state(99.0, 100.0)},
            "s9_15m_completed": {"NIFTY 50": state(99.0, 100.0)},
        }

        direction, signal = ScreenerEngine.__new__(ScreenerEngine)._resolve_s9_direction_from_trend_states(
            states_by_timeframe=states,
            underlying_symbol="NIFTY 50",
        )

        self.assertEqual((direction, signal), ("bearish", "strong_sell"))


if __name__ == "__main__":
    unittest.main()
