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


def _pt(ts: datetime, close: float) -> IndicatorPoint:
    return IndicatorPoint(
        candle_time=ts,
        open=float(close),
        high=float(close),
        low=float(close),
        close=float(close),
        volume=1.0,
        volume_ma=1.0,
        rsi=55.0,
        macd=2.0,
        macd_signal=1.0,
        macd_histogram=1.0,
        mcginley=close,
        sma=close,
        bias="neutral",
        strength=0.5,
    )


class TestS3Contributors(unittest.TestCase):
    def test_points_formula_basic(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            nifty_symbols=("AAA", "BBB"),
            nifty50_shares_iwf={
                "AAA": {"shares": 100.0, "iwf": 1.0},
                "BBB": {"shares": 100.0, "iwf": 1.0},
            },
        )
        engine = ScreenerEngine(settings)

        t0 = datetime(2026, 5, 1, 9, 15)
        t1 = datetime(2026, 5, 1, 9, 20)

        bucket = {
            "NIFTY 50": SymbolIndicatorState("NIFTY 50", "5m", latest=_pt(t1, 10100), previous=_pt(t0, 10000)),
            "AAA": SymbolIndicatorState("AAA", "5m", latest=_pt(t1, 101), previous=_pt(t0, 100)),  # +1%
            "BBB": SymbolIndicatorState("BBB", "5m", latest=_pt(t1, 99), previous=_pt(t0, 100)),  # -1%
        }
        weights, status = engine._s3_weights_at_start(bucket=bucket, symbols=["AAA", "BBB"])  # noqa: SLF001
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(weights["AAA"] + weights["BBB"], 100.0, places=4)

        out = engine._run_s3({"5m": bucket})[0]  # noqa: SLF001
        tf = out.payload["timeframes"]["5m"]
        self.assertEqual(tf["index"]["total_move_points"], 100.0)
        combined = tf["combined"]
        self.assertTrue(any(row["symbol"] == "AAA" for row in combined))


if __name__ == "__main__":
    unittest.main()
