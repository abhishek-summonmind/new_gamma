from __future__ import annotations

"""Service wrapper exposing refresh and S1 signal APIs."""

from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from .refresh_service import RefreshService


class ScreenerService:
    def __init__(self, settings: Settings | None = None):
        self._refresh = RefreshService(settings)

    def run_scan(self, source: str = "manual", force: bool = True) -> dict[str, Any]:
        return self._refresh.run_refresh(trigger=source, force=force)

    def refresh(self, *, force: bool = True) -> dict[str, Any]:
        return self._refresh.run_refresh(trigger="manual", force=force)

    def get_s1_signals(self, *, limit: int = 50) -> dict[str, Any]:
        return self._refresh.get_screener_results(screener_code="S1", limit=limit)

    def get_latest_payload(self, _db: Session | None = None) -> dict[str, Any] | None:
        return self._refresh.get_latest_run()

    def get_run_payload(self, _db: Session, _run_id: int) -> dict[str, Any]:
        latest = self._refresh.get_latest_run()
        return latest or {}

    def list_run_history(self, _db: Session, limit: int = 20) -> list[dict[str, Any]]:
        history = self._refresh.get_legacy_history(limit=limit)
        return history.get("items", []) if isinstance(history, dict) else []

    def get_runtime_config(self) -> dict[str, Any]:
        return {
            "refresh_interval_seconds": self._refresh._settings.refresh_interval_seconds,  # noqa: SLF001
            "timeframes": list(self._refresh._settings.timeframes),  # noqa: SLF001
            "market_timezone": self._refresh._settings.market_timezone,  # noqa: SLF001
        }
