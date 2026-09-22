import os
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.alert_engine import AlertEngine  # noqa: E402
from app.services.screener_engine import ScreenerSignal  # noqa: E402


class TestAlertEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = AlertEngine(Settings(database_url="sqlite:///./test.db"))

    def test_builds_per_screener_alerts(self) -> None:
        rows = {
            "S1": [
                ScreenerSignal(
                    screener="S1",
                    symbol="DIXON",
                    signal="sell",
                    confidence=0.72,
                    reason="Bearish confirmation on 5m.",
                    payload={},
                )
            ],
            "S9": [
                ScreenerSignal(
                    screener="S9",
                    symbol="DIXON",
                    signal="strong_buy",
                    confidence=0.66,
                    reason="S9 option confirmation passed all filters.",
                    payload={},
                )
            ],
        }

        alerts = self.engine.build_alerts(rows)
        self.assertEqual(len(alerts), 2)

        a0 = alerts[0]
        self.assertIn(a0.action, {"buy", "sell"})
        self.assertTrue(isinstance(a0.payload, dict))
        self.assertTrue(isinstance(a0.payload.get("sources"), list))

        by_screener = {a.payload.get("source_screener"): a for a in alerts}
        self.assertEqual(by_screener["S1"].action, "sell")
        self.assertEqual(by_screener["S9"].action, "buy")
        self.assertEqual(by_screener["S1"].payload.get("source_screener"), "S1")
        self.assertEqual(by_screener["S9"].payload.get("source_screener"), "S9")

