import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.api import routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.services.option_types import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.refresh_service import RefreshService  # noqa: E402
from app.services.screener_engine import ScreenerSignal  # noqa: E402


def _row(
    symbol: str,
    score: int,
    passed: int,
    *,
    row_id: int,
    strike: int = 100,
    option_type: str = "CE",
    expiry: str = "2026-09-24",
    gtp: float | None = None,
    gtp_time: str | None = None,
    gtp_pass_count: int | None = None,
    gtp_total_filters: int | None = None,
    ltp: float | None = None,
    signal_time: str | None = None,
) -> dict:
    snapshot_passed = passed if gtp_pass_count is None else gtp_pass_count
    snapshot_total = 8 if gtp_total_filters is None else gtp_total_filters
    return {
        "id": row_id,
        "symbol": symbol,
        "signal": "neutral",
        "payload": {
            "underlying_symbol": symbol,
            "score": score,
            "passed_count": passed,
            "total_filters": 8,
            "evaluated_strike": strike,
            "evaluated_option_type": option_type,
            "strike_scan": {"expiry": expiry},
            "gtp": gtp,
            "gtp_time": gtp_time,
            "gtp_pass": f"{snapshot_passed}/{snapshot_total}",
            "gtp_pass_count": snapshot_passed,
            "gtp_total_filters": snapshot_total,
            "ltp": ltp,
            "signal_time": signal_time,
        },
    }


class TestS9TopOpportunities(unittest.TestCase):
    def test_universe_keeps_nifty_sensex_and_scans_bank_finnifty_stocks(self) -> None:
        original_s9 = routes.settings.s9_scan_symbols
        original_nifty = routes.settings.nifty_symbols
        try:
            routes.settings.s9_scan_symbols = ("BANK NIFTY", "FINNIFTY")  # type: ignore[misc]
            routes.settings.nifty_symbols = ("RELIANCE", "ICICIBANK", "NIFTY 50")  # type: ignore[misc]

            universe = routes._s9_scan_universe()
        finally:
            routes.settings.s9_scan_symbols = original_s9  # type: ignore[misc]
            routes.settings.nifty_symbols = original_nifty  # type: ignore[misc]

        self.assertEqual(universe[:2], ["NIFTY 50", "SENSEX"])
        self.assertIn("BANK NIFTY", universe)
        self.assertIn("FINNIFTY", universe)
        self.assertIn("RELIANCE", universe)
        self.assertIn("ICICIBANK", universe)
        self.assertEqual(len(universe), len(set(universe)))

    def test_top_six_has_mandatory_indices_and_highest_ranked_non_index_symbols(self) -> None:
        items = [
            _row("NIFTY 50", 10, 2, row_id=1, strike=22000),
            _row("SENSEX", 5, 1, row_id=2, strike=72000),
            _row("RELIANCE", 70, 6, row_id=3),
            _row("ICICIBANK", 90, 8, row_id=4),
            _row("BANK NIFTY", 80, 7, row_id=5),
            _row("FINNIFTY", 60, 5, row_id=6),
            _row("SBIN", 50, 4, row_id=7),
        ]

        ordered, diagnostics = routes._rank_s9_top_opportunities(items, limit=6)
        symbols = [routes._s9_item_symbol(item) for item in ordered]

        self.assertEqual(symbols, ["ICICIBANK", "BANK NIFTY", "RELIANCE", "FINNIFTY", "NIFTY 50", "SENSEX"])
        self.assertEqual(len(ordered), 6)
        self.assertIn("ICICIBANK", symbols)
        self.assertIn("BANK NIFTY", symbols)
        self.assertIn("RELIANCE", symbols)
        self.assertIn("FINNIFTY", symbols)
        self.assertNotIn("SBIN", symbols)
        self.assertEqual(diagnostics["candidate_count"], len(items))

    def test_higher_current_score_replaces_previous_lower_ranked_stock(self) -> None:
        old_items = [
            _row("NIFTY 50", 10, 2, row_id=1),
            _row("SENSEX", 10, 2, row_id=2),
            _row("RELIANCE", 80, 7, row_id=3),
            _row("ICICIBANK", 70, 6, row_id=4),
            _row("SBIN", 60, 5, row_id=5),
            _row("BANK NIFTY", 50, 4, row_id=6),
        ]
        new_items = [
            _row("NIFTY 50", 10, 2, row_id=7),
            _row("SENSEX", 10, 2, row_id=8),
            _row("RELIANCE", 80, 7, row_id=9),
            _row("ICICIBANK", 70, 6, row_id=10),
            _row("SBIN", 60, 5, row_id=11),
            _row("DIXON", 95, 8, row_id=12),
        ]

        old_ordered, _ = routes._rank_s9_top_opportunities(old_items, limit=6)
        new_ordered, _ = routes._rank_s9_top_opportunities(new_items, limit=6)
        old_symbols = {routes._s9_item_symbol(item) for item in old_ordered}
        new_symbols = {routes._s9_item_symbol(item) for item in new_ordered}

        self.assertIn("BANK NIFTY", old_symbols)
        self.assertIn("DIXON", new_symbols)
        self.assertNotIn("BANK NIFTY", new_symbols)

    def test_dynamic_slots_are_strictly_sorted_by_current_score(self) -> None:
        items = [
            _row("NIFTY 50", 1, 1, row_id=1),
            _row("SENSEX", 1, 1, row_id=2),
            _row("RELIANCE", 80, 1, row_id=3),
            _row("BANK NIFTY", 70, 8, row_id=4),
            _row("FINNIFTY", 60, 7, row_id=5),
            _row("DIXON", 50, 6, row_id=6),
            _row("HDFCAMC", 50, 8, row_id=7),
            _row("ICICIBANK", 49, 8, row_id=8),
        ]

        ordered, _ = routes._rank_s9_top_opportunities(items, limit=6)
        symbols = [routes._s9_item_symbol(item) for item in ordered]

        self.assertEqual(symbols[:4], ["RELIANCE", "BANK NIFTY", "FINNIFTY", "DIXON"])
        self.assertEqual(symbols[4:], ["NIFTY 50", "SENSEX"])
        self.assertNotIn("ICICIBANK", symbols)

    def test_score_beats_passed_count_and_row_id_for_dynamic_ranking(self) -> None:
        items = [
            _row("NIFTY 50", 1, 1, row_id=1),
            _row("SENSEX", 1, 1, row_id=2),
            _row("DIXON", 91, 1, row_id=999),
            _row("HDFCAMC", 92, 0, row_id=3),
            _row("RELIANCE", 80, 8, row_id=4),
            _row("BANK NIFTY", 70, 8, row_id=5),
            _row("FINNIFTY", 60, 8, row_id=6),
        ]

        ordered, _ = routes._rank_s9_top_opportunities(items, limit=6)
        symbols = [routes._s9_item_symbol(item) for item in ordered]

        self.assertEqual(symbols[:4], ["HDFCAMC", "DIXON", "RELIANCE", "BANK NIFTY"])
        self.assertEqual(symbols[4:], ["NIFTY 50", "SENSEX"])

    def test_route_wires_full_ranking_and_ltp_polling_to_top_opportunities_methods(self) -> None:
        original_service = routes.refresh_service
        original_attach = routes._attach_s9_trade_status
        fake_service = _FakeRouteRefreshService()
        try:
            routes.refresh_service = fake_service  # type: ignore[assignment]
            routes._attach_s9_trade_status = lambda items: None  # type: ignore[assignment]

            full_payload = routes.screener_s9(limit=6, symbol=None, refresh=True)
            ltp_payload = routes.screener_s9(limit=6, symbol=None, refresh=False, ltp_refresh=True)
            cached_payload = routes.screener_s9(limit=6, symbol=None, refresh=False, ltp_refresh=False)
        finally:
            routes.refresh_service = original_service  # type: ignore[assignment]
            routes._attach_s9_trade_status = original_attach  # type: ignore[assignment]

        self.assertEqual(full_payload["source"], "full")
        self.assertEqual(ltp_payload["source"], "ltp")
        self.assertEqual(cached_payload["source"], "cached")
        self.assertEqual(fake_service.calls, ["full", "ltp", "cached"])

    def test_legacy_single_symbol_ltp_cache_helper_is_not_exposed_for_top_flow(self) -> None:
        self.assertFalse(hasattr(RefreshService, "refresh_s9_ltp_cache"))

    def test_best_strike_per_instrument_uses_current_highest_ranked_row(self) -> None:
        items = [
            _row("NIFTY 50", 1, 1, row_id=1),
            _row("SENSEX", 1, 1, row_id=2),
            _row("RELIANCE", 40, 4, row_id=3, strike=100),
            _row("RELIANCE", 90, 8, row_id=4, strike=110, option_type="PE"),
            _row("ICICIBANK", 80, 7, row_id=5),
        ]

        ordered, diagnostics = routes._rank_s9_top_opportunities(items, limit=6)
        reliance = next(item for item in ordered if routes._s9_item_symbol(item) == "RELIANCE")

        self.assertEqual(reliance["payload"]["evaluated_strike"], 110)
        self.assertEqual(reliance["payload"]["evaluated_option_type"], "PE")
        selected_reliance = next(item for item in diagnostics["selected"] if item["symbol"] == "RELIANCE")
        self.assertEqual(selected_reliance["strike"], 110)

    def test_refresh_service_full_rebuild_replaces_stale_company_without_restart(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            s9_scan_symbols=("BANK NIFTY", "FINNIFTY"),
            nifty_symbols=("RELIANCE", "ICICIBANK", "DIXON"),
        )
        service = RefreshService(settings)
        previous_items = [
            _row("NIFTY 50", 10, 2, row_id=1),
            _row("SENSEX", 10, 2, row_id=2),
            _row("RELIANCE", 80, 7, row_id=3),
            _row("ICICIBANK", 70, 6, row_id=4),
            _row("FINNIFTY", 60, 5, row_id=5),
            _row("BANK NIFTY", 50, 4, row_id=6),
        ]
        current_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 10, 2, row_id=11)],
            "SENSEX": [_row("SENSEX", 10, 2, row_id=12)],
            "RELIANCE": [_row("RELIANCE", 80, 7, row_id=13)],
            "ICICIBANK": [_row("ICICIBANK", 70, 6, row_id=14)],
            "FINNIFTY": [_row("FINNIFTY", 60, 5, row_id=15)],
            "BANK NIFTY": [_row("BANK NIFTY", 50, 4, row_id=16)],
            "DIXON": [_row("DIXON", 95, 8, row_id=17)],
        }

        cache = _FakeCache({
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "items": previous_items,
                "count": len(previous_items),
            }
        })
        service._cache = cache  # noqa: SLF001
        service._s9_scan_universe = lambda symbol=None: list(current_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeS9Service(symbol, current_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_opportunities(limit=6)
        symbols = {RefreshService._s9_item_symbol(item) for item in payload["items"]}  # noqa: SLF001
        diagnostics = payload["diagnostics"]

        self.assertIn("DIXON", symbols)
        self.assertNotIn("BANK NIFTY", symbols)
        self.assertIn("DIXON", diagnostics["replacements"]["added"])
        self.assertIn("BANK NIFTY", diagnostics["replacements"]["removed"])
        self.assertEqual(diagnostics["candidate_count"], len(current_by_symbol))
        self.assertIsNotNone(cache.data[service._s9_top_opportunities_cache_key()]["cache_version"])  # noqa: SLF001

    def test_full_refresh_replaces_existing_top4_with_previously_absent_higher_score(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                s9_scan_symbols=("BANK NIFTY", "FINNIFTY"),
                nifty_symbols=("RELIANCE", "DIXON", "HDFCAMC", "ICICIBANK"),
            )
        )
        previous_items = [
            _row("NIFTY 50", 1, 1, row_id=1),
            _row("SENSEX", 1, 1, row_id=2),
            _row("RELIANCE", 80, 8, row_id=3),
            _row("BANK NIFTY", 70, 8, row_id=4),
            _row("FINNIFTY", 60, 8, row_id=5),
            _row("DIXON", 50, 8, row_id=6),
        ]
        current_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 1, 1, row_id=11)],
            "SENSEX": [_row("SENSEX", 1, 1, row_id=12)],
            "RELIANCE": [_row("RELIANCE", 80, 8, row_id=13)],
            "BANK NIFTY": [_row("BANK NIFTY", 70, 8, row_id=14)],
            "FINNIFTY": [_row("FINNIFTY", 60, 8, row_id=15)],
            "DIXON": [_row("DIXON", 50, 8, row_id=16)],
            "HDFCAMC": [_row("HDFCAMC", 55, 1, row_id=17)],
            "ICICIBANK": [_row("ICICIBANK", 49, 8, row_id=18)],
        }
        service._cache = _FakeCache({  # noqa: SLF001
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "items": previous_items,
                "count": len(previous_items),
            }
        })
        service._s9_scan_universe = lambda symbol=None: list(current_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeS9Service(symbol, current_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_opportunities(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertEqual(symbols, ["RELIANCE", "BANK NIFTY", "FINNIFTY", "HDFCAMC", "NIFTY 50", "SENSEX"])
        self.assertNotIn("DIXON", symbols)

    def test_auto_full_refresh_updates_top_opportunities_in_running_service_when_scores_change(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                refresh_interval_seconds=10,
                s9_scan_symbols=("BANK NIFTY", "FINNIFTY"),
                nifty_symbols=("RELIANCE", "DIXON", "HDFCAMC"),
            )
        )
        service._cache = _FakeCache()  # noqa: SLF001
        current_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 1, 1, row_id=1)],
            "SENSEX": [_row("SENSEX", 1, 1, row_id=2)],
            "RELIANCE": [_row("RELIANCE", 80, 8, row_id=3)],
            "BANK NIFTY": [_row("BANK NIFTY", 70, 8, row_id=4)],
            "FINNIFTY": [_row("FINNIFTY", 60, 8, row_id=5)],
            "DIXON": [_row("DIXON", 50, 8, row_id=6)],
            "HDFCAMC": [_row("HDFCAMC", 49, 8, row_id=7)],
        }
        service._s9_scan_universe = lambda symbol=None: list(current_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeS9Service(symbol, current_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        first_time = datetime(2026, 9, 15, 10, 0)
        first = service.refresh_s9_top_opportunities_if_due(now_market=first_time, limit=6)
        first_symbols = [RefreshService._s9_item_symbol(item) for item in first["payload"]["items"]]  # noqa: SLF001
        self.assertEqual(first["status"], "completed")
        self.assertIn("DIXON", first_symbols)
        self.assertNotIn("HDFCAMC", first_symbols)

        current_by_symbol["HDFCAMC"] = [_row("HDFCAMC", 55, 1, row_id=17)]
        skipped = service.refresh_s9_top_opportunities_if_due(now_market=first_time + timedelta(seconds=30), limit=6)
        cached_symbols = [
            RefreshService._s9_item_symbol(item)  # noqa: SLF001
            for item in service.get_s9_top_opportunities(limit=6)["items"]
        ]
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("DIXON", cached_symbols)
        self.assertNotIn("HDFCAMC", cached_symbols)

        second = service.refresh_s9_top_opportunities_if_due(now_market=first_time + timedelta(seconds=181), limit=6)
        second_symbols = [RefreshService._s9_item_symbol(item) for item in second["payload"]["items"]]  # noqa: SLF001
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second_symbols, ["RELIANCE", "BANK NIFTY", "FINNIFTY", "HDFCAMC", "NIFTY 50", "SENSEX"])
        self.assertNotIn("DIXON", second_symbols)

    def test_s9_auto_refresh_is_due_every_three_minutes(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db", refresh_interval_seconds=10))

        self.assertEqual(service._s9_top_auto_refresh_interval_seconds(), 180)  # noqa: SLF001

    def test_full_refresh_skips_failed_instrument_and_continues_market_scan(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        rows_by_symbol = {
            "SENSEX": [_row("SENSEX", 60, 5, row_id=2, strike=72000)],
            "RELIANCE": [_row("RELIANCE", 90, 8, row_id=3, strike=2800)],
        }
        service._cache = _FakeCache()  # noqa: SLF001
        service._s9_scan_universe = lambda symbol=None: ["NIFTY 50", "SENSEX", "RELIANCE"]  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: (  # type: ignore[method-assign]  # noqa: SLF001
            _FailingS9Service(symbol)
            if symbol == "NIFTY 50"
            else _FakeS9Service(symbol, rows_by_symbol[symbol])
        )

        payload = service.refresh_s9_top_opportunities(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertIn("NIFTY 50", payload["errors"])
        self.assertIn("SENSEX", symbols)
        self.assertIn("RELIANCE", symbols)
        self.assertEqual(payload["runs"]["NIFTY 50"], None)

    def test_full_refresh_preserves_historical_gtp_time_for_same_contract(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                s9_scan_symbols=("BANK NIFTY", "FINNIFTY"),
                nifty_symbols=("RELIANCE",),
            )
        )
        previous_time = "2026-09-15T09:31:00"
        new_time = "2026-09-15T09:32:00"
        previous_items = [
            _row("NIFTY 50", 10, 2, row_id=1, strike=22000),
            _row("SENSEX", 10, 2, row_id=2, strike=72000),
            _row("RELIANCE", 90, 8, row_id=3, strike=2800, option_type="CE", gtp_time=previous_time),
        ]
        current_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 10, 2, row_id=11, strike=22000)],
            "SENSEX": [_row("SENSEX", 10, 2, row_id=12, strike=72000)],
            "RELIANCE": [_row("RELIANCE", 90, 8, row_id=13, strike=2800, option_type="CE", gtp_time=new_time)],
            "BANK NIFTY": [_row("BANK NIFTY", 70, 8, row_id=14, strike=50000)],
            "FINNIFTY": [_row("FINNIFTY", 60, 8, row_id=15, strike=24000)],
        }
        service._cache = _FakeCache({  # noqa: SLF001
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "cache_version": datetime.now().isoformat(),
                "items": previous_items,
                "count": len(previous_items),
            }
        })
        service._s9_scan_universe = lambda symbol=None: list(current_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeS9Service(symbol, current_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_opportunities(limit=6)
        reliance = next(item for item in payload["items"] if RefreshService._s9_item_symbol(item) == "RELIANCE")  # noqa: SLF001

        self.assertEqual(reliance["payload"]["gtp_time"], previous_time)
        self.assertEqual(payload["gtp_first_seen"]["RELIANCE|2026-09-24|2800|CE"]["gtp_time"], previous_time)

    def test_gtp_snapshot_triplet_stays_immutable_while_live_fields_change(self) -> None:
        service = RefreshService(
            Settings(
                database_url="sqlite:///./test.db",
                s9_scan_symbols=("BANK NIFTY", "FINNIFTY"),
                nifty_symbols=("RELIANCE",),
            )
        )
        service._cache = _FakeCache()  # noqa: SLF001
        first_time = "2026-09-15T09:31:00"
        second_time = "2026-09-15T09:32:00"
        current_by_symbol = {
            "NIFTY 50": [_row("NIFTY 50", 10, 2, row_id=11, strike=22000)],
            "SENSEX": [_row("SENSEX", 10, 2, row_id=12, strike=72000)],
            "RELIANCE": [
                _row(
                    "RELIANCE",
                    90,
                    8,
                    row_id=13,
                    strike=100,
                    option_type="CE",
                    gtp=100.25,
                    gtp_time=first_time,
                    gtp_pass_count=8,
                    gtp_total_filters=8,
                    ltp=101.0,
                    signal_time=first_time,
                )
            ],
            "BANK NIFTY": [_row("BANK NIFTY", 70, 8, row_id=14, strike=50000)],
            "FINNIFTY": [_row("FINNIFTY", 60, 8, row_id=15, strike=24000)],
        }
        service._s9_scan_universe = lambda symbol=None: list(current_by_symbol)  # type: ignore[method-assign]  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeS9Service(symbol, current_by_symbol[symbol])  # type: ignore[method-assign]  # noqa: SLF001

        first_payload = service.refresh_s9_top_opportunities(limit=6)
        first_reliance = next(item for item in first_payload["items"] if RefreshService._s9_item_symbol(item) == "RELIANCE")  # noqa: SLF001
        first_snapshot = {
            "gtp": first_reliance["payload"]["gtp"],
            "gtp_time": first_reliance["payload"]["gtp_time"],
            "gtp_pass": first_reliance["payload"]["gtp_pass"],
            "gtp_pass_count": first_reliance["payload"]["gtp_pass_count"],
            "gtp_total_filters": first_reliance["payload"]["gtp_total_filters"],
        }

        current_by_symbol["RELIANCE"] = [
            _row(
                "RELIANCE",
                91,
                7,
                row_id=23,
                strike=100,
                option_type="CE",
                gtp=110.75,
                gtp_time=second_time,
                gtp_pass_count=7,
                gtp_total_filters=8,
                ltp=112.0,
                signal_time=second_time,
            )
        ]

        second_payload = service.refresh_s9_top_opportunities(limit=6)
        second_reliance = next(item for item in second_payload["items"] if RefreshService._s9_item_symbol(item) == "RELIANCE")  # noqa: SLF001
        second_payload_fields = second_reliance["payload"]

        self.assertEqual(second_payload_fields["gtp"], first_snapshot["gtp"])
        self.assertEqual(second_payload_fields["gtp_time"], first_snapshot["gtp_time"])
        self.assertEqual(second_payload_fields["gtp_pass"], first_snapshot["gtp_pass"])
        self.assertEqual(second_payload_fields["gtp_pass_count"], first_snapshot["gtp_pass_count"])
        self.assertEqual(second_payload_fields["gtp_total_filters"], first_snapshot["gtp_total_filters"])
        self.assertEqual(second_payload_fields["ltp"], 112.0)
        self.assertEqual(second_payload_fields["passed_count"], 7)
        self.assertEqual(second_payload_fields["score"], 91)
        self.assertEqual(second_payload_fields["signal_time"], second_time)

        now_market = datetime.now(service._market_tz)  # noqa: SLF001
        service._s9_top_last_full_refresh_at = now_market  # noqa: SLF001
        service._s9_top_next_auto_refresh_at = now_market + timedelta(minutes=1)  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeChainService(symbol)  # type: ignore[method-assign]  # noqa: SLF001
        ltp_payload = service.refresh_s9_top_ltp_cache(limit=6)
        ltp_reliance = next(item for item in ltp_payload["items"] if RefreshService._s9_item_symbol(item) == "RELIANCE")  # noqa: SLF001
        ltp_payload_fields = ltp_reliance["payload"]

        self.assertEqual(ltp_payload_fields["gtp"], first_snapshot["gtp"])
        self.assertEqual(ltp_payload_fields["gtp_time"], first_snapshot["gtp_time"])
        self.assertEqual(ltp_payload_fields["gtp_pass"], first_snapshot["gtp_pass"])
        self.assertEqual(ltp_payload_fields["gtp_pass_count"], first_snapshot["gtp_pass_count"])
        self.assertEqual(ltp_payload_fields["gtp_total_filters"], first_snapshot["gtp_total_filters"])
        self.assertEqual(ltp_payload_fields["ltp"], 101.5)
        self.assertEqual(ltp_payload_fields["passed_count"], 7)
        self.assertEqual(ltp_payload_fields["score"], 91)
        self.assertEqual(ltp_payload_fields["signal_time"], second_time)

    def test_stored_s9_signal_reuses_first_qualifying_gtp_after_signal_loss_and_requalification(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        first_time = "2026-09-15T09:31:00"
        neutral_time = "2026-09-15T09:35:00"
        second_time = "2026-09-15T09:42:00"
        first_payload = _row(
            "RELIANCE",
            90,
            8,
            row_id=1,
            strike=100,
            option_type="CE",
            gtp=100.25,
            gtp_time=first_time,
            gtp_pass_count=8,
            gtp_total_filters=8,
            ltp=101.0,
            signal_time=first_time,
        )["payload"]
        first_payload["confirmed"] = True
        neutral_payload = _row(
            "RELIANCE",
            65,
            5,
            row_id=2,
            strike=100,
            option_type="CE",
            gtp=108.5,
            gtp_time=neutral_time,
            gtp_pass_count=5,
            gtp_total_filters=8,
            ltp=109.0,
            signal_time=neutral_time,
        )["payload"]
        neutral_payload["confirmed"] = False
        db = _FakeScreenerDb(
            [
                _StoredS9Row("RELIANCE", "BUY_CALL", 0.9, "S9_FILTER_RANKED_MATCH", first_payload),
                _StoredS9Row("RELIANCE", "neutral", 0.0, "SWEEP_FAILED", neutral_payload),
            ]
        )
        requalified_payload = _row(
            "RELIANCE",
            91,
            7,
            row_id=3,
            strike=100,
            option_type="CE",
            gtp=112.75,
            gtp_time=second_time,
            gtp_pass_count=7,
            gtp_total_filters=8,
            ltp=113.0,
            signal_time=second_time,
        )["payload"]
        requalified_payload["confirmed"] = True
        row = ScreenerSignal(
            screener="S9",
            symbol="RELIANCE",
            signal="BUY_CALL",
            confidence=0.91,
            reason="S9_FILTER_RANKED_MATCH",
            payload=requalified_payload,
        )

        service._apply_s9_first_signal_gtp_snapshot(db=db, row=row)  # noqa: SLF001

        self.assertEqual(requalified_payload["gtp"], 100.25)
        self.assertEqual(requalified_payload["gtp_time"], first_time)
        self.assertEqual(requalified_payload["gtp_pass"], "8/8")
        self.assertEqual(requalified_payload["gtp_pass_count"], 8)
        self.assertEqual(requalified_payload["gtp_total_filters"], 8)
        self.assertEqual(requalified_payload["ltp"], 113.0)
        self.assertEqual(requalified_payload["passed_count"], 7)
        self.assertEqual(requalified_payload["score"], 91)
        self.assertEqual(requalified_payload["signal_time"], second_time)

    def test_refresh_service_ltp_refresh_does_not_rebuild_or_reorder_ranking(self) -> None:
        settings = Settings(database_url="sqlite:///./test.db")
        service = RefreshService(settings)
        last_full_refresh_at = datetime.now(service._market_tz)  # noqa: SLF001
        next_due_at = last_full_refresh_at + timedelta(minutes=5)
        cache_version = datetime.now().isoformat()
        service._s9_top_last_full_refresh_at = last_full_refresh_at  # noqa: SLF001
        service._s9_top_next_auto_refresh_at = next_due_at  # noqa: SLF001
        items = [
            _row("NIFTY 50", 10, 2, row_id=1, strike=22000),
            _row("SENSEX", 10, 2, row_id=2, strike=72000),
            _row("RELIANCE", 90, 8, row_id=3, strike=100),
            _row("DIXON", 80, 7, row_id=4, strike=200),
        ]
        cache = _FakeCache({
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "items": items,
                "count": len(items),
                "cache_version": cache_version,
                "cache_run_id": 99,
            }
        })
        service._cache = cache  # noqa: SLF001
        service._s9_service_for_symbol = lambda symbol: _FakeChainService(symbol)  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_ltp_cache(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertEqual(symbols, ["NIFTY 50", "SENSEX", "RELIANCE", "DIXON"])
        self.assertEqual(payload["cache_version"], cache_version)
        self.assertTrue(payload["ltp_refresh"]["ranking_unchanged"])
        self.assertEqual(service._s9_top_last_full_refresh_at, last_full_refresh_at)  # noqa: SLF001
        self.assertEqual(service._s9_top_next_auto_refresh_at, next_due_at)  # noqa: SLF001
        reliance_payload = next(item["payload"] for item in payload["items"] if item["symbol"] == "RELIANCE")
        self.assertEqual(reliance_payload["ltp"], 101.5)

    def test_ltp_refresh_preserves_cached_ranking_order(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        now = datetime.now(service._market_tz)  # noqa: SLF001
        service._s9_top_last_full_refresh_at = now  # noqa: SLF001
        service._s9_top_next_auto_refresh_at = now + timedelta(minutes=1)  # noqa: SLF001
        items = [
            _row("BANK NIFTY", 70, 8, row_id=1, strike=50000),
            _row("RELIANCE", 90, 8, row_id=2, strike=100),
            _row("NIFTY 50", 10, 2, row_id=3, strike=22000),
            _row("SENSEX", 10, 2, row_id=4, strike=72000),
        ]
        service._cache = _FakeCache({  # noqa: SLF001
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "items": items,
                "count": len(items),
                "cache_version": datetime.now().isoformat(),
                "cache_run_id": 99,
            }
        })
        service._s9_service_for_symbol = lambda symbol: _FakeChainService(symbol)  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_ltp_cache(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertEqual(symbols, ["BANK NIFTY", "RELIANCE", "NIFTY 50", "SENSEX"])

    def test_cached_top_opportunities_always_include_nifty_and_sensex(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        cache = _FakeCache({
            service._s9_top_opportunities_cache_key(): {  # noqa: SLF001
                "cache_version": datetime.now().isoformat(),
                "count": 2,
                "items": [
                    _row("RELIANCE", 80, 4, row_id=1),
                    _row("DIXON", 75, 3, row_id=2),
                ],
            }
        })
        service._cache = cache  # noqa: SLF001

        payload = service.get_s9_top_opportunities(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertEqual(symbols, ["RELIANCE", "DIXON", "NIFTY 50", "SENSEX"])
        self.assertIn("RELIANCE", symbols)
        self.assertIn("DIXON", symbols)

    def test_ltp_refresh_bootstraps_background_scan_when_top_cache_missing(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        cache = _FakeCache()
        service._cache = cache  # noqa: SLF001
        scheduled: list[dict[str, Any]] = []

        def fake_schedule(*, limit: int, reason: str) -> None:
            scheduled.append({"limit": limit, "reason": reason})

        service._schedule_s9_top_opportunities_refresh = fake_schedule  # type: ignore[method-assign]  # noqa: SLF001

        payload = service.refresh_s9_top_ltp_cache(limit=6)
        symbols = [RefreshService._s9_item_symbol(item) for item in payload["items"]]  # noqa: SLF001

        self.assertEqual(symbols, ["NIFTY 50", "SENSEX"])
        self.assertEqual(payload["refresh_type"], "bootstrap_pending")
        self.assertEqual(payload["ltp_refresh"]["status"], "bootstrap_scheduled")
        self.assertEqual(scheduled, [{"limit": 6, "reason": "missing_top_opportunities_cache"}])

    def test_latest_completed_run_can_disable_symbol_scope_for_market_wide_s9(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        db = _StatementCaptureDb()

        service._latest_completed_run(db, screener_code="S9", symbol_scope=None)  # noqa: SLF001
        compiled = str(db.statement.compile(compile_kwargs={"literal_binds": True}))

        self.assertIn("screener_results.screener_code = 'S9'", compiled)
        self.assertNotIn("screener_results.symbol =", compiled)


class _FakeCache:
    def __init__(self, data: dict | None = None):
        self.data = data or {}

    def get_json(self, key: str):
        return self.data.get(key)

    def set_json(self, key: str, value, ttl_seconds: int | None = None):
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


class _FakeS9Service:
    def __init__(self, symbol: str, rows: list[dict]):
        self.symbol = symbol
        self.rows = rows

    def run_refresh(self, **_: object) -> dict:
        return {"status": "completed", "run_id": abs(hash(self.symbol)) % 10000}

    def get_screener_results(self, **_: object) -> dict:
        return {"run": {"id": abs(hash(self.symbol)) % 10000}, "items": self.rows}


class _FailingS9Service:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def run_refresh(self, **_: object) -> dict:
        raise RuntimeError(f"option-chain retries exhausted for {self.symbol}")


class _FakeChainService:
    def __init__(self, symbol: str):
        self._ingestion = self
        self.symbol = symbol

    def fetch_option_chain_snapshot(self) -> OptionChainSnapshot:
        strike = {
            "NIFTY 50": 22000,
            "SENSEX": 72000,
            "RELIANCE": 100,
            "DIXON": 200,
        }.get(self.symbol, 100)
        return OptionChainSnapshot(
            underlying=self.symbol,
            spot_price=float(strike),
            atm_strike=int(strike),
            expiry_date=date(2026, 1, 1),
            snapshot_time=datetime(2026, 1, 1, 10, 0),
            contracts=(
                OptionContract(
                    security_id=f"{self.symbol}-{strike}-CE",
                    strike=float(strike),
                    option_type="CE",
                    ltp=101.5,
                    oi=1000,
                    oi_change=1,
                    volume=100,
                ),
            ),
        )


class _StoredS9Row:
    def __init__(self, symbol: str, signal: str, confidence: float, reason: str, payload: dict):
        self.symbol = symbol
        self.signal = signal
        self.confidence = confidence
        self.reason = reason
        self.payload = payload


class _FakeScreenerDb:
    def __init__(self, rows: list[_StoredS9Row]):
        self.rows = rows

    def scalars(self, statement):  # noqa: ARG002
        return self.rows


class _FakeRouteRefreshService:
    def __init__(self):
        self.calls: list[str] = []

    def refresh_s9_top_opportunities(self, *, limit: int) -> dict:
        self.calls.append("full")
        return {"source": "full", "items": [], "count": 0}

    def refresh_s9_top_ltp_cache(self, *, limit: int) -> dict:
        self.calls.append("ltp")
        return {"source": "ltp", "items": [], "count": 0}

    def get_s9_top_opportunities(self, *, limit: int) -> dict:
        self.calls.append("cached")
        return {"source": "cached", "items": [], "count": 0}


class _StatementCaptureDb:
    statement = None

    def scalar(self, statement):
        self.statement = statement
        return None


if __name__ == "__main__":
    unittest.main()
