from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from backend.app.config import Settings  # noqa: E402
from backend.app.services.refresh_service import RefreshService  # noqa: E402


def _row(symbol: str, score: int, row_id: int, *, strike: int, option_type: str = "CE") -> dict:
    return {
        "id": row_id,
        "symbol": symbol,
        "signal": "BUY_CALL" if option_type == "CE" else "BUY_PUT",
        "confidence": score / 100,
        "reason": "latest scan",
        "payload": {
            "underlying_symbol": symbol,
            "evaluated_strike": strike,
            "evaluated_option_type": option_type,
            "score": score,
            "passed_count": 7,
            "total_filters": 8,
            "status": "latest",
            "strike_scan": {"expiry": "2026-09-24"},
        },
    }


class _Cache:
    def __init__(self, data: dict | None = None):
        self.data = data or {}

    def get_json(self, key: str):
        return self.data.get(key)

    def set_json(self, key: str, value, ttl_seconds: int | None = None):  # noqa: ARG002
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class _ScanService:
    def __init__(self, symbol: str, rows: list[dict]):
        self.symbol = symbol
        self.rows = rows

    def run_refresh(self, **_: object) -> dict:
        return {"status": "completed"}

    def get_screener_results(self, **_: object) -> dict:
        return {
            "run": {
                "id": abs(hash(self.symbol)) % 10000,
                "started_at": "2026-09-21T10:00:00+05:30",
                "completed_at": "2026-09-21T10:00:01+05:30",
            },
            "items": self.rows,
        }


class TestS9RankingRefreshCycle(unittest.TestCase):
    def test_ranking_interval_is_independently_configurable(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                refresh_interval_seconds=10,
                s9_top_refresh_interval_seconds=180,
            )
        )

        self.assertEqual(service._s9_top_auto_refresh_interval_seconds(), 180)  # noqa: SLF001

    def test_due_ltp_poll_cannot_overwrite_completed_full_scan_ranking(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                s9_top_refresh_interval_seconds=180,
                s9_scan_symbols=("BANK NIFTY",),
                nifty_symbols=("DIXON", "HDFCAMC"),
            )
        )
        old_items = [
            _row("NIFTY 50", 10, 1, strike=23000),
            _row("SENSEX", 10, 2, strike=74000),
            _row("DIXON", 90, 3, strike=15000),
        ]
        cache_key = service._s9_top_opportunities_cache_key()  # noqa: SLF001
        cache = _Cache(
            {
                cache_key: {
                    "cache_version": "2026-09-21T09:57:00+05:30",
                    "cache_run_id": 1,
                    "count": len(old_items),
                    "items": old_items,
                    "runs": {"NIFTY 50": {"completed_at": "2026-09-21T09:57:00+05:30"}},
                }
            }
        )
        service._cache = cache  # noqa: SLF001
        service._s9_top_next_auto_refresh_at = datetime.now(service._market_tz) - timedelta(seconds=1)  # noqa: SLF001

        latest_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 20, 11, strike=23100, option_type="PE")],
            "SENSEX": [_row("SENSEX", 25, 12, strike=74100, option_type="PE")],
            "BANK NIFTY": [_row("BANK NIFTY", 80, 13, strike=54000)],
            "HDFCAMC": [_row("HDFCAMC", 95, 15, strike=2600, option_type="PE")],
        }
        service._s9_scan_universe = lambda symbol=None: list(latest_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _ScanService(symbol, latest_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        def complete_full_scan_now(*, limit: int, reason: str) -> dict:
            self.assertEqual(reason, "ltp_poll_ranking_due")
            service.refresh_s9_top_opportunities(limit=limit)
            return {
                "status": "scheduled",
                "reason": reason,
                "next_refresh_at": service._s9_top_next_auto_refresh_at.isoformat(),  # noqa: SLF001
            }

        service._schedule_s9_top_opportunities_refresh_if_due = complete_full_scan_now  # type: ignore[method-assign]  # noqa: SLF001

        with self.assertLogs("backend.app.services.refresh_service", level="INFO") as captured:
            payload = service.refresh_s9_top_ltp_cache(limit=6)

        symbols = [item["symbol"] for item in payload["items"]]
        self.assertIn("HDFCAMC", symbols)
        self.assertNotIn("DIXON", symbols)
        hdfc = next(item for item in payload["items"] if item["symbol"] == "HDFCAMC")
        self.assertEqual(hdfc["payload"]["evaluated_strike"], 2600)
        self.assertEqual(hdfc["payload"]["evaluated_option_type"], "PE")
        self.assertEqual(hdfc["payload"]["score"], 95)
        self.assertEqual(hdfc["payload"]["status"], "latest")
        self.assertEqual(payload["ltp_refresh"]["status"], "superseded_by_full_scan")
        self.assertEqual(cache.data[cache_key]["items"], payload["items"])
        self.assertIsNotNone(payload["cache_replaced_at"])
        self.assertIsNotNone(payload["ranking_refresh"]["next_refresh_at"])

        logs = "\n".join(captured.output)
        self.assertIn("S9_TOP_FULL_REFRESH_CYCLE_START", logs)
        self.assertIn("S9_TOP_FULL_SCAN_CANDIDATES", logs)
        self.assertIn("S9_TOP_CACHE_REPLACED", logs)
        self.assertIn("old_top_6=", logs)
        self.assertIn("new_top_6=", logs)
        self.assertIn("next_ranking_refresh_at=", logs)
        self.assertIn("discarded_stale_write", logs)


if __name__ == "__main__":
    unittest.main()
