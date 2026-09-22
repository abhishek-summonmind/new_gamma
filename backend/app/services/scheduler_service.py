from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .refresh_service import RefreshService

logger = logging.getLogger(__name__)


class RefreshScheduler:
    def __init__(self, refresh_service: RefreshService, interval_seconds: int = 300):
        self._refresh_service = refresh_service
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_scheduled_close: datetime | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._runner(), name="intraday-refresh-scheduler")
        self._task.add_done_callback(self._log_task_exit)
        logger.info(
            "Refresh scheduler started with %s second interval service_id=%s cache_id=%s task=%s",
            self._interval_seconds,
            id(self._refresh_service),
            id(self._refresh_service._cache),  # noqa: SLF001
            self._task.get_name(),
        )

    async def stop(self) -> None:
        if self._task is None:
            return

        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            # Uvicorn/Starlette may cancel lifespan during shutdown (e.g., Ctrl+C).
            # Avoid surfacing noisy errors when we're already stopping.
            return
        self._task = None
        logger.info("Refresh scheduler stopped")

    def _log_task_exit(self, task: asyncio.Task) -> None:
        if self._stop_event.is_set():
            return

        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.warning("Refresh scheduler task was cancelled unexpectedly")
            return
        except Exception:  # noqa: BLE001
            logger.exception("Failed to inspect refresh scheduler task result")
            return

        if exc is None:
            logger.warning("Refresh scheduler task exited unexpectedly without error")
            return

        logger.exception("Refresh scheduler task exited unexpectedly", exc_info=exc)

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid HH:MM value: {value}")
        return int(parts[0]), int(parts[1])

    @staticmethod
    def _timeframe_minutes(value: str) -> int:
        text = str(value).strip().lower()
        if not text.endswith("m"):
            raise ValueError(f"Unsupported timeframe: {value}")
        return int(text[:-1])

    def _next_close_time(self, now_market: datetime) -> datetime:
        settings = self._refresh_service._settings  # noqa: SLF001
        tz = ZoneInfo(settings.market_timezone)

        now_local = now_market.astimezone(tz) if now_market.tzinfo else now_market.replace(tzinfo=tz)
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        open_h, open_m = self._parse_hhmm(settings.market_open_time)
        open_time = day_start + timedelta(hours=open_h, minutes=open_m)
        close_h, close_m = self._parse_hhmm(settings.market_close_time)
        close_time = day_start + timedelta(hours=close_h, minutes=close_m)

        step_seconds = int(self._refresh_service._s9_top_auto_refresh_interval_seconds())  # noqa: SLF001
        step = timedelta(seconds=max(1, step_seconds))

        if now_local < open_time:
            return open_time

        if now_local >= close_time:
            return day_start + timedelta(days=1) + (open_time - day_start)

        elapsed = now_local - open_time
        ticks_elapsed = int(elapsed.total_seconds() // step.total_seconds())
        candidate = open_time + (ticks_elapsed + 1) * step

        # Include the market-close tick (15:30 by default), then wait for next session.
        if candidate > close_time:
            return day_start + timedelta(days=1) + (open_time - day_start)

        return candidate

    async def _runner(self) -> None:
        # Keep the scheduler alive even if configuration parsing or refresh logic throws.
        # Any exception that leaks out of this coroutine would permanently stop intraday updates.
        while not self._stop_event.is_set():
            try:
                settings = self._refresh_service._settings  # noqa: SLF001
                tz = ZoneInfo(settings.market_timezone)
                now_market = datetime.now(tz)
                next_close = self._next_close_time(now_market)

                # Avoid double-triggering the same close when the loop wakes up late.
                if self._last_scheduled_close is not None and next_close <= self._last_scheduled_close:
                    next_close = self._last_scheduled_close + timedelta(seconds=1)

                delay_seconds = max(0.0, (next_close - now_market).total_seconds())
                logger.info("Next scheduled refresh at %s (in %.1fs)", next_close.isoformat(), delay_seconds)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
                    continue
                except asyncio.TimeoutError:
                    pass

                cycle_started = time.monotonic()
                try:
                    # Never refresh after market hours. If the process wakes up late (sleep drift),
                    # skip this cycle and let the loop schedule the next valid close.
                    now_market = datetime.now(tz)
                    if not self._refresh_service.is_market_open(now_market):
                        logger.info("Market closed at %s; skipping scheduled refresh.", now_market.isoformat())
                        self._last_scheduled_close = next_close
                        continue

                    # Check the market-wide S9 ranking on every scheduler cycle. Keep this
                    # independent of the normal scan so a failed normal refresh cannot
                    # silently suppress S9 indefinitely.
                    before = self._refresh_service.s9_top_refresh_schedule_state(now_market=now_market)
                    s9_result: dict | None = None
                    try:
                        s9_result = await asyncio.to_thread(
                            self._refresh_service.refresh_s9_top_opportunities_if_due,
                            now_market=now_market,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "S9_SCHEDULER_TICK current_time=%s last_full_refresh_at=%s next_due_at=%s "
                            "due=%s action=failed service_id=%s cache_id=%s",
                            before["current_time"],
                            before["last_full_refresh_at"],
                            before["next_due_at"],
                            str(before["due"]).lower(),
                            id(self._refresh_service),
                            id(self._refresh_service._cache),  # noqa: SLF001
                        )
                    else:
                        action = "started" if s9_result.get("status") == "completed" else "skipped"
                        logger.info(
                            "S9_SCHEDULER_TICK current_time=%s last_full_refresh_at=%s next_due_at=%s "
                            "due=%s action=%s status=%s reason=%s service_id=%s cache_id=%s",
                            before["current_time"],
                            before["last_full_refresh_at"],
                            before["next_due_at"],
                            str(before["due"]).lower(),
                            action,
                            s9_result.get("status"),
                            s9_result.get("reason"),
                            id(self._refresh_service),
                            id(self._refresh_service._cache),  # noqa: SLF001
                        )

                    await asyncio.to_thread(self._refresh_service.run_refresh, trigger="scheduler", force=False)
                    self._last_scheduled_close = next_close
                except Exception:  # noqa: BLE001
                    logger.exception("Scheduled refresh cycle failed")

                elapsed = time.monotonic() - cycle_started
                if elapsed > 0.5:
                    logger.info("Scheduled refresh completed in %.2fs", elapsed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # If something goes wrong (e.g. bad env var), don't kill the scheduler forever.
                logger.exception("Refresh scheduler loop crashed; retrying shortly")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
