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
from app.services.indicator_engine import SymbolIndicatorState  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402
from app.utils.indicators import IndicatorPoint  # noqa: E402


def _point(*, close, mcginley, rsi, macd, macd_signal):
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
        mcginley=None if mcginley is None else float(mcginley),
        sma=None,
        bias="neutral",
        strength=0.5,
    )


def _state(timeframe: str, prev: IndicatorPoint | None, curr: IndicatorPoint) -> SymbolIndicatorState:
    return SymbolIndicatorState(symbol="DIXON", timeframe=timeframe, latest=curr, previous=prev)


class TestS1Cascade(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(database_url="sqlite:///./test.db", nifty_symbols=("DIXON",))
        self.engine = ScreenerEngine(settings)

    def test_bullish_confirmation(self) -> None:
        prev = _point(close=99, mcginley=100, rsi=49, macd=0, macd_signal=0)
        curr = _point(close=101, mcginley=100, rsi=51, macd=2, macd_signal=1)
        direction, detail = self.engine._s1_cascade(_state("5m", prev, curr))  # noqa: SLF001
        self.assertEqual(direction, "bullish")
        self.assertEqual(detail.get("status"), "confirmed")

    def test_no_fresh_cross_neutral(self) -> None:
        prev = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        curr = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1)
        direction, detail = self.engine._s1_cascade(_state("5m", prev, curr))  # noqa: SLF001
        self.assertEqual(direction, "neutral")
        self.assertEqual(detail.get("status"), "no_fresh_cross")

    def test_rsi_gate_fail(self) -> None:
        prev = _point(close=99, mcginley=100, rsi=49, macd=2, macd_signal=1)
        curr = _point(close=101, mcginley=100, rsi=50, macd=2, macd_signal=1)
        direction, detail = self.engine._s1_cascade(_state("5m", prev, curr))  # noqa: SLF001
        self.assertEqual(direction, "neutral")
        self.assertEqual(detail.get("status"), "rsi_gate_failed")

    def test_action_strong_buy_needs_10m_and_15m(self) -> None:
        prev = _point(close=99, mcginley=100, rsi=49, macd=0, macd_signal=0)
        curr = _point(close=101, mcginley=100, rsi=51, macd=2, macd_signal=1)
        s5 = _state("5m", prev, curr)
        s10 = _state("10m", prev, curr)
        s15 = _state("15m", prev, curr)

        signal, _confidence, _reason, payload = self.engine._resolve_s1_signal(  # noqa: SLF001
            symbol="DIXON",
            state_5m=s5,
            state_10m=s10,
            state_15m=s15,
        )
        self.assertEqual(signal, "strong_buy")
        self.assertEqual(payload["signals"]["10m"], "bullish")
        self.assertEqual(payload["signals"]["15m"], "bullish")

    def test_recent_strong_signal_time_persists_for_15m(self) -> None:
        prev = _point(close=99, mcginley=100, rsi=49, macd=0, macd_signal=0)
        curr = _point(close=101, mcginley=100, rsi=51, macd=2, macd_signal=1)
        s5 = _state("5m", prev, curr)
        s10 = _state("10m", prev, curr)
        s15 = _state("15m", prev, curr)

        signal, _confidence, _reason, payload = self.engine._resolve_s1_signal(  # noqa: SLF001
            symbol="DIXON",
            state_5m=s5,
            state_10m=s10,
            state_15m=s15,
            now_market=datetime(2026, 5, 1, 9, 21, 0),
        )
        self.assertEqual(signal, "strong_buy")
        self.assertIsNotNone(payload.get("signal_time"))
        self.assertEqual(payload.get("last_strong_action"), "strong_buy")

        prev2 = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        curr2 = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1)
        s5b = _state("5m", prev2, curr2)
        s10b = _state("10m", prev2, curr2)
        s15b = _state("15m", prev2, curr2)

        signal2, _confidence2, _reason2, payload2 = self.engine._resolve_s1_signal(  # noqa: SLF001
            symbol="DIXON",
            state_5m=s5b,
            state_10m=s10b,
            state_15m=s15b,
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(signal2, "watch")
        self.assertIsNotNone(payload2.get("signal_time"))
        self.assertEqual(payload2.get("last_strong_action"), "strong_buy")

    def test_run_s1_includes_watch_rows(self) -> None:
        prev = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        curr = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1)
        states_by_timeframe = {
            "5m": {"DIXON": _state("5m", prev, curr)},
            "10m": {"DIXON": _state("10m", prev, curr)},
            "15m": {"DIXON": _state("15m", prev, curr)},
        }
        rows = self.engine._run_s1(states_by_timeframe, now_market=datetime(2026, 5, 1, 9, 30, 0))  # noqa: SLF001
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "DIXON")
        self.assertEqual(rows[0].signal, "watch")


if __name__ == "__main__":
    unittest.main()
