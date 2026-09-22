import sys
import unittest
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.utils.indicators import build_indicator_frame, rsi_series


class TestRsiInputWindow(unittest.TestCase):
    def test_rsi_uses_only_latest_50_input_candles(self):
        index = pd.date_range("2026-09-21 09:15", periods=70, freq="15min", tz="Asia/Kolkata")
        frame = pd.DataFrame(
            {
                "open": range(100, 170),
                "high": range(101, 171),
                "low": range(99, 169),
                "close": range(100, 170),
                "volume": [1000.0] * 70,
            },
            index=index,
        )
        rsi_input = frame.tail(50)

        result = build_indicator_frame(frame, rsi_frame=rsi_input)
        expected = rsi_series(rsi_input["close"], length=14).iloc[-1]

        self.assertEqual(len(rsi_input), 50)
        self.assertAlmostEqual(float(result["rsi"].iloc[-1]), float(expected))

    def test_rsi_input_can_exclude_current_incomplete_bar(self):
        index = pd.date_range("2026-09-21 09:15", periods=51, freq="15min", tz="Asia/Kolkata")
        frame = pd.DataFrame(
            {
                "open": range(100, 151),
                "high": range(101, 152),
                "low": range(99, 150),
                "close": range(100, 151),
                "volume": [1000.0] * 51,
            },
            index=index,
        )
        completed = frame.iloc[:-1].tail(50)

        result = build_indicator_frame(frame, rsi_frame=completed)
        expected = rsi_series(completed["close"], length=14).iloc[-1]

        self.assertAlmostEqual(float(result["rsi"].iloc[-1]), float(expected))


if __name__ == "__main__":
    unittest.main()