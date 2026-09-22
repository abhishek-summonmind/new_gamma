from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from backend.app.config import Settings  # noqa: E402
from backend.app.services.refresh_service import RefreshService  # noqa: E402
from backend.app.services.scheduler_service import RefreshScheduler  # noqa: E402


class TestRefreshSchedulerCadence(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_timezone="Asia/Kolkata",
            market_open_time="09:15",
            market_close_time="15:30",
            timeframes=("5m", "15m"),
        )
        service = RefreshService(settings)
        self.scheduler = RefreshScheduler(service, interval_seconds=10)
        self.tz = ZoneInfo("Asia/Kolkata")

    def test_first_tick_is_market_open(self) -> None:
        now = datetime(2026, 9, 15, 9, 0, tzinfo=self.tz)

        self.assertEqual(
            self.scheduler._next_close_time(now),  # noqa: SLF001
            datetime(2026, 9, 15, 9, 15, tzinfo=self.tz),
        )

    def test_ticks_every_three_minutes_after_open(self) -> None:
        now = datetime(2026, 9, 15, 9, 16, 30, tzinfo=self.tz)

        self.assertEqual(
            self.scheduler._next_close_time(now),  # noqa: SLF001
            datetime(2026, 9, 15, 9, 18, tzinfo=self.tz),
        )

    def test_market_close_tick_is_included(self) -> None:
        now = datetime(2026, 9, 15, 15, 29, 30, tzinfo=self.tz)

        self.assertEqual(
            self.scheduler._next_close_time(now),  # noqa: SLF001
            datetime(2026, 9, 15, 15, 30, tzinfo=self.tz),
        )

    def test_after_market_close_schedules_next_open(self) -> None:
        now = datetime(2026, 9, 15, 15, 30, 1, tzinfo=self.tz)

        self.assertEqual(
            self.scheduler._next_close_time(now),  # noqa: SLF001
            datetime(2026, 9, 16, 9, 15, tzinfo=self.tz),
        )


if __name__ == "__main__":
    unittest.main()
