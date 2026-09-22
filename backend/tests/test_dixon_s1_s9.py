import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings
from app.services.screener_engine import ScreenerEngine, ScreenerSignal
from app.services.indicator_engine import SymbolIndicatorState
from app.utils.indicators import IndicatorPoint


def _point(*, close, rsi, macd, macd_signal):
    return IndicatorPoint(
        candle_time=datetime(2026, 5, 1, 9, 20, 0),
        open=float(close),
        high=float(close),
        low=float(close),
        close=float(close),
        volume=1.0,
        volume_ma=None,
        rsi=None if rsi is None else float(rsi),
        macd=None if macd is None else float(macd),
        macd_signal=None if macd_signal is None else float(macd_signal),
        macd_histogram=None,
        mcginley=float(close),
        sma=None,
        bias="neutral",
        strength=0.5,
    )


def _state(timeframe: str, prev: IndicatorPoint | None, curr: IndicatorPoint) -> SymbolIndicatorState:
    return SymbolIndicatorState(symbol="DIXON", timeframe=timeframe, latest=curr, previous=prev)


class TestDixonS1S9(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            nifty_index_symbol="DIXON",
            nifty_symbols=("DIXON",),
        )
        self.engine = ScreenerEngine(settings)

    def test_s9_uses_dixon_s1_signal_for_direction(self) -> None:
        states = {
            "5m": {"DIXON": _state("5m", _point(close=100, rsi=40, macd=0, macd_signal=0), _point(close=102, rsi=56, macd=1, macd_signal=0.2))},
            "10m": {"DIXON": _state("10m", _point(close=100, rsi=40, macd=0, macd_signal=0), _point(close=102, rsi=56, macd=1, macd_signal=0.2))},
            "15m": {"DIXON": _state("15m", _point(close=100, rsi=40, macd=0, macd_signal=0), _point(close=102, rsi=56, macd=1, macd_signal=0.2))},
        }
        s1_signals = [
            ScreenerSignal(screener="S1", symbol="DIXON", signal="strong_buy", confidence=0.95, reason="", payload={}),
        ]

        rows = self.engine._run_s9(
            states_by_timeframe=states,
            s1_signals=s1_signals,
            option_chain=None,
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(len(rows), 1)
        payload = rows[0].payload
        self.assertEqual(payload["underlying_symbol"], "DIXON")
        self.assertEqual(payload["effective_direction"], "bullish")
        self.assertEqual(payload["effective_signal"], "strong_buy")
        self.assertEqual(payload["direction_source"], "auto")
        self.assertEqual(payload["rejection_reason"], "OPTION_CHAIN_UNAVAILABLE")

    def test_s9_uses_neutral_when_dixon_s1_is_neutral(self) -> None:
        states = {
            "5m": {"DIXON": _state("5m", _point(close=100, rsi=50, macd=0, macd_signal=0), _point(close=101, rsi=50, macd=0, macd_signal=0))},
            "10m": {"DIXON": _state("10m", _point(close=100, rsi=50, macd=0, macd_signal=0), _point(close=101, rsi=50, macd=0, macd_signal=0))},
            "15m": {"DIXON": _state("15m", _point(close=100, rsi=50, macd=0, macd_signal=0), _point(close=101, rsi=50, macd=0, macd_signal=0))},
        }
        s1_signals = [
            ScreenerSignal(screener="S1", symbol="DIXON", signal="watch", confidence=0.3, reason="", payload={}),
        ]

        rows = self.engine._run_s9(
            states_by_timeframe=states,
            s1_signals=s1_signals,
            option_chain=None,
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(len(rows), 1)
        payload = rows[0].payload
        self.assertEqual(payload["underlying_symbol"], "DIXON")
        self.assertEqual(payload["effective_direction"], "neutral")
        self.assertEqual(payload["effective_signal"], "neutral")
        self.assertEqual(payload["rejection_reason"], "DIXON_S1_NEUTRAL")


if __name__ == "__main__":
    unittest.main()
