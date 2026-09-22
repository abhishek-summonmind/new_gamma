from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from backend import main  # noqa: E402
from backend.app.api import routes  # noqa: E402


class TestS9SchedulerApplicationIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_running_fastapi_lifespan_automatically_refreshes_s9_across_ticks(self) -> None:
        """Exercise the real FastAPI lifespan and scheduler task without restarting either."""
        service = main.refresh_service
        scheduler = main.scheduler
        original_startup_setting = main.settings.run_refresh_on_startup
        original_interval = service._s9_top_auto_refresh_interval_seconds  # noqa: SLF001
        original_next_close = scheduler._next_close_time  # noqa: SLF001
        original_is_market_open = service.is_market_open
        original_run_refresh = service.run_refresh
        original_full_refresh = service.refresh_s9_top_opportunities
        original_last = service._s9_top_last_full_refresh_at  # noqa: SLF001
        original_next = service._s9_top_next_auto_refresh_at  # noqa: SLF001

        scheduler_ticks = 0
        full_refreshes = 0

        def next_close(now_market: datetime) -> datetime:
            return now_market + timedelta(milliseconds=20)

        def normal_refresh(**_: object) -> dict[str, str]:
            nonlocal scheduler_ticks
            scheduler_ticks += 1
            return {"status": "completed"}

        def full_refresh(*, limit: int = 6) -> dict[str, object]:
            nonlocal full_refreshes
            full_refreshes += 1
            items = [
                {
                    "symbol": f"TEST-{index}",
                    "payload": {"underlying_symbol": f"TEST-{index}", "score": 10 - index},
                }
                for index in range(limit)
            ]
            return {
                "status": "completed",
                "cache_version": f"integration-{full_refreshes}",
                "cache_run_id": full_refreshes,
                "items": items,
            }

        try:
            main.settings.run_refresh_on_startup = False
            service._s9_top_last_full_refresh_at = None  # noqa: SLF001
            service._s9_top_next_auto_refresh_at = None  # noqa: SLF001
            service._s9_top_auto_refresh_interval_seconds = lambda: 0.04  # type: ignore[method-assign]  # noqa: SLF001
            service.is_market_open = lambda now_market=None: True  # type: ignore[method-assign]
            service.run_refresh = normal_refresh  # type: ignore[method-assign]
            service.refresh_s9_top_opportunities = full_refresh  # type: ignore[method-assign]
            scheduler._next_close_time = next_close  # type: ignore[method-assign]  # noqa: SLF001
            scheduler._last_scheduled_close = None  # noqa: SLF001

            with (
                patch.object(main, "validate_market_data_credentials"),
                patch.object(main, "init_db"),
                patch.object(main, "validate_live_market_data_connection"),
                self.assertLogs("backend.app.services.scheduler_service", level="INFO") as captured,
            ):
                async with main.lifespan(main.app):
                    self.assertIs(main.scheduler._refresh_service, main.refresh_service)  # noqa: SLF001
                    self.assertIs(main.refresh_service, routes.refresh_service)
                    await asyncio.sleep(0.18)

            logs = "\n".join(captured.output)
            self.assertGreaterEqual(scheduler_ticks, 3)
            self.assertGreaterEqual(full_refreshes, 2)
            self.assertIn("S9_SCHEDULER_TICK", logs)
            self.assertIn("action=started", logs)
            self.assertIn("action=skipped", logs)
        finally:
            main.settings.run_refresh_on_startup = original_startup_setting
            service._s9_top_auto_refresh_interval_seconds = original_interval  # type: ignore[method-assign]  # noqa: SLF001
            service.is_market_open = original_is_market_open  # type: ignore[method-assign]
            service.run_refresh = original_run_refresh  # type: ignore[method-assign]
            service.refresh_s9_top_opportunities = original_full_refresh  # type: ignore[method-assign]
            service._s9_top_last_full_refresh_at = original_last  # noqa: SLF001
            service._s9_top_next_auto_refresh_at = original_next  # noqa: SLF001
            scheduler._next_close_time = original_next_close  # type: ignore[method-assign]  # noqa: SLF001
            scheduler._last_scheduled_close = None  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
