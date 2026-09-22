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
from app.services.cache_service import CacheService  # noqa: E402
from app.services.indicator_engine import IndicatorComputationResult, SymbolIndicatorState  # noqa: E402
from app.services.price_rsi_divergence_service import PriceRsiDivergenceService  # noqa: E402
from app.services.screener_engine import ScreenerEngine  # noqa: E402
from app.utils.indicators import IndicatorPoint  # noqa: E402


def _point(*, candle_time: datetime, close: float, rsi: float | None) -> IndicatorPoint:
    return IndicatorPoint(
        candle_time=candle_time,
        open=float(close),
        high=float(close),
        low=float(close),
        close=float(close),
        volume=1.0,
        volume_ma=None,
        rsi=None if rsi is None else float(rsi),
        macd=None,
        macd_signal=None,
        macd_histogram=None,
        mcginley=None,
        sma=None,
        bias="neutral",
        strength=0.5,
    )


def _state(timeframe: str, symbol: str, prev: IndicatorPoint | None, curr: IndicatorPoint) -> SymbolIndicatorState:
    return SymbolIndicatorState(symbol=symbol, timeframe=timeframe, latest=curr, previous=prev)


class TestS5PriceRsiDivergence(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(database_url="sqlite:///./test.db", redis_url="memory://")

    def test_screener_engine_emits_fake_buy_and_fake_sell(self) -> None:
        engine = ScreenerEngine(self.settings)

        t1 = datetime(2026, 5, 1, 9, 20, 0)
        prev = _point(candle_time=t1, close=100.0, rsi=55.0)
        curr_buy = _point(candle_time=datetime(2026, 5, 1, 9, 25, 0), close=101.0, rsi=54.0)
        curr_sell = _point(candle_time=datetime(2026, 5, 1, 9, 30, 0), close=99.0, rsi=56.0)

        states_by_timeframe = {
            "5m": {
                "RELIANCE": _state("5m", "RELIANCE", prev, curr_buy),
            },
            "10m": {
                "RELIANCE": _state("10m", "RELIANCE", prev, curr_sell),
            },
            "15m": {},
        }

        signals = engine._run_s5(states_by_timeframe=states_by_timeframe)  # noqa: SLF001
        self.assertTrue(any(row.symbol == "RELIANCE" and row.payload.get("timeframe") == "5m" and row.signal == "fake_buy" for row in signals))
        self.assertTrue(any(row.symbol == "RELIANCE" and row.payload.get("timeframe") == "10m" and row.signal == "fake_sell" for row in signals))

    def test_screener_engine_threshold_filters_tiny_moves(self) -> None:
        engine = ScreenerEngine(self.settings)

        prev = _point(candle_time=datetime(2026, 5, 1, 9, 20, 0), close=100.0, rsi=55.0)
        curr = _point(candle_time=datetime(2026, 5, 1, 9, 25, 0), close=100.2, rsi=54.0)  # +0.2%

        states_by_timeframe = {"5m": {"RELIANCE": _state("5m", "RELIANCE", prev, curr)}}
        signals = engine._run_s5(states_by_timeframe=states_by_timeframe)  # noqa: SLF001
        self.assertEqual(signals, [])

    def test_snapshot_signal_time_updates_only_on_action_change(self) -> None:
        cache = CacheService(self.settings)
        service = PriceRsiDivergenceService(self.settings, cache=cache)

        # Force tiny universe for test determinism.
        service._settings.nifty_symbols = ("RELIANCE",)  # type: ignore[attr-defined]  # noqa: SLF001

        t1 = datetime(2026, 5, 1, 9, 20, 0)
        t2 = datetime(2026, 5, 1, 9, 25, 0)
        t3 = datetime(2026, 5, 1, 9, 30, 0)

        prev = _point(candle_time=t1, close=100.0, rsi=55.0)
        curr1 = _point(candle_time=t1, close=101.0, rsi=54.0)
        curr2 = _point(candle_time=t2, close=102.0, rsi=53.0)  # same action (fake_buy)
        curr3 = _point(candle_time=t3, close=103.0, rsi=54.0)  # action becomes watch (RSI up)

        indicators = IndicatorComputationResult(primary_timeframe="5m")
        indicators.states_by_timeframe["5m"] = {"RELIANCE": _state("5m", "RELIANCE", prev, curr1)}

        service.store_snapshots(indicators=indicators, now_market=t1)
        snap1 = service.get_snapshot(timeframe="5m")
        row1 = snap1["items"][0]
        self.assertEqual(row1["action"], "fake_buy")
        self.assertEqual(row1["signal_time"], t1.isoformat())

        indicators.states_by_timeframe["5m"] = {"RELIANCE": _state("5m", "RELIANCE", curr1, curr2)}
        service.store_snapshots(indicators=indicators, now_market=t2)
        snap2 = service.get_snapshot(timeframe="5m")
        row2 = snap2["items"][0]
        self.assertEqual(row2["action"], "fake_buy")
        self.assertEqual(row2["signal_time"], t1.isoformat())

        indicators.states_by_timeframe["5m"] = {"RELIANCE": _state("5m", "RELIANCE", curr2, curr3)}
        service.store_snapshots(indicators=indicators, now_market=t3)
        snap3 = service.get_snapshot(timeframe="5m")
        row3 = snap3["items"][0]
        self.assertEqual(row3["action"], "watch")
        self.assertEqual(row3["signal_time"], t3.isoformat())


if __name__ == "__main__":
    unittest.main()

