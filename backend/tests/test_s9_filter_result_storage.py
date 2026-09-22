from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from backend.app.config import Settings  # noqa: E402
from backend.app.db import Base  # noqa: E402
from backend.app.models import S9FilterResult, ScreenerResult  # noqa: E402
from backend.app.services.refresh_service import RefreshService  # noqa: E402
from backend.app.services.screener_engine import ScreenerSignal  # noqa: E402


class _CollectingDb:
    def __init__(self) -> None:
        self.added: list[object] = []

    def flush(self) -> None:
        return None

    def add(self, row: object) -> None:
        self.added.append(row)

    def scalar(self, _statement):
        return None


class TestS9FilterResultStorage(unittest.TestCase):
    def test_s9_filter_rows_persist_ltp_in_dedicated_column(self) -> None:
        service = RefreshService(Settings(database_url="sqlite:///./test.db"))
        db = _CollectingDb()
        signal = ScreenerSignal(
            screener="S9",
            symbol="NIFTY 50",
            signal="BUY_CALL",
            confidence=0.9,
            reason="test",
            payload={
                "signal_time": "2026-09-15T09:15:00",
                "selected_strike": 25000,
                "selected_option_type": "CE",
                "scanned_strikes": [
                    {
                        "strike": 25000,
                        "option_type": "CE",
                        "ltp": 123.45,
                        "score": 80,
                        "filters": [{"key": "delta", "passed": True}],
                    }
                ],
            },
        )

        service._store_s9_filter_rows(  # noqa: SLF001
            db=db,
            run_id=1,
            screener_result=ScreenerResult(id=10),
            row=signal,
        )

        [stored] = [row for row in db.added if isinstance(row, S9FilterResult)]
        self.assertEqual(stored.ltp, 123.45)
        self.assertNotIn("ltp", stored.data)

    def test_s9_filter_serializer_exposes_last_known_ltp(self) -> None:
        row = S9FilterResult(
            id=1,
            run_id=1,
            symbol="NIFTY 50",
            signal="BUY_CALL",
            strike=25000,
            option_type="CE",
            filter_name="strike",
            passed=True,
            data={"ltp": 123.45, "score": 80},
            ltp=123.45,
            score=80,
        )

        payload = RefreshService._serialize_s9_filter_result(row)  # noqa: SLF001

        self.assertEqual(payload["ltp"], 123.45)
        self.assertEqual(payload["score"], 80)


class TestS9ConsolidatedPersistence(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.service = RefreshService(Settings(database_url="sqlite://"))

    @staticmethod
    def _filters() -> list[dict]:
        return [
            {"key": "sweep", "passed": True, "actual_value": 101.5, "data": {"close": 101.5, "ema9": 100.2}},
            {"key": "stoch_rsi", "passed": False, "actual_value": 72.0, "data": {"stoch_rsi_k": 72.0, "stoch_rsi_d": 76.0}},
            {"key": "supertrend", "passed": True, "actual_value": 98.4, "data": {"supertrend": 98.4, "direction": "bullish"}},
            {"key": "delta", "passed": True, "actual_value": 0.54, "data": {"delta": 0.54}},
            {"key": "pcr", "passed": False, "actual_value": 0.82, "data": {"pcr": 0.82}},
            {"key": "vwap", "passed": True, "actual_value": 99.7, "data": {"vwap": 99.7, "premium": 101.5}},
            {"key": "order_book", "passed": True, "actual_value": 1.35, "data": {"bid_imbalance": 1.35, "ask_imbalance": 0.74}},
            {
                "key": "volume_breakout",
                "passed": False,
                "actual_value": 1.2,
                "data": {"volume": 1200, "avg20": 1000, "volume_ratio": 1.2, "required_multiplier": 1.5},
            },
        ]

    def _signal(self, rows: list[dict]) -> ScreenerSignal:
        return ScreenerSignal(
            screener="S9",
            symbol="NIFTY 50",
            signal="BUY_CALL",
            confidence=0.75,
            reason="test",
            payload={
                "signal_time": "2026-09-21T09:15:00",
                "selected_strike": rows[0]["strike"],
                "selected_option_type": rows[0]["option_type"],
                "scanned_strikes": rows,
            },
        )

    def _row(self, strike: int, option_type: str, *, score: int = 75) -> dict:
        return {
            "strike": strike,
            "option_type": option_type,
            "option_symbol": f"NIFTY{strike}{option_type}",
            "score": score,
            "max_score": 100,
            "passed_filter_count": 5,
            "required_filter_count": 8,
            "all_filters_passed": False,
            "filters": self._filters(),
        }

    def _store(self, db, *, run_id: int, rows: list[dict], result_id: int) -> None:
        self.service._store_s9_filter_rows(  # noqa: SLF001
            db=db,
            run_id=run_id,
            screener_result=ScreenerResult(id=result_id),
            row=self._signal(rows),
        )
        db.flush()

    def test_one_row_per_ce_pe_and_strike_with_all_values_and_scores(self) -> None:
        with self.Session() as db:
            rows = [self._row(23300, "CE"), self._row(23300, "PE"), self._row(23350, "CE")]
            self._store(db, run_id=10, rows=rows, result_id=100)

            stored = list(db.scalars(select(S9FilterResult).order_by(S9FilterResult.strike, S9FilterResult.option_type)))
            self.assertEqual(len(stored), 3)
            self.assertEqual({(item.strike, item.option_type) for item in stored}, {(23300.0, "CE"), (23300.0, "PE"), (23350.0, "CE")})
            ce = next(item for item in stored if item.strike == 23300 and item.option_type == "CE")
            self.assertTrue(ce.sweep_ema9_pass)
            self.assertFalse(ce.stoch_rsi_pass)
            self.assertTrue(ce.supertrend_pass)
            self.assertTrue(ce.delta_pass)
            self.assertFalse(ce.pcr_pass)
            self.assertTrue(ce.vwap_pass)
            self.assertTrue(ce.order_book_pass)
            self.assertFalse(ce.volume_breakout_pass)
            self.assertEqual(ce.stoch_rsi_value["stoch_rsi_k"], 72.0)
            self.assertEqual(ce.stoch_rsi_value["stoch_rsi_d"], 76.0)
            self.assertEqual(ce.supertrend_value["direction"], "bullish")
            self.assertEqual(ce.delta_value["delta"], 0.54)
            self.assertEqual(ce.pcr_value["pcr"], 0.82)
            self.assertEqual(ce.vwap_value["vwap"], 99.7)
            self.assertEqual(ce.order_book_value["bid_imbalance"], 1.35)
            self.assertEqual(ce.volume_breakout_value["avg20"], 1000)
            self.assertEqual((ce.score, ce.max_score, ce.passed_count, ce.total_filters), (75, 100, 5, 8))

    def test_same_run_upserts_while_new_refresh_run_inserts_new_record(self) -> None:
        with self.Session() as db:
            self._store(db, run_id=10, rows=[self._row(23300, "CE", score=75)], result_id=100)
            self._store(db, run_id=10, rows=[self._row(23300, "CE", score=88)], result_id=101)
            self.assertEqual(db.scalar(select(func.count()).select_from(S9FilterResult)), 1)
            self.assertEqual(db.scalar(select(S9FilterResult.score)), 88)

            self._store(db, run_id=11, rows=[self._row(23300, "CE", score=80)], result_id=102)
            self.assertEqual(db.scalar(select(func.count()).select_from(S9FilterResult)), 2)

    def test_database_unique_index_blocks_duplicate_consolidated_identity(self) -> None:
        with self.Session() as db:
            common = dict(run_id=10, symbol="NIFTY 50", strike=23300, option_type="CE", filter_name="strike")
            db.add(S9FilterResult(**common))
            db.flush()
            db.add(S9FilterResult(**common))
            with self.assertRaises(IntegrityError):
                db.flush()

    def test_zero_calculation_counts_are_not_changed_to_null(self) -> None:
        row = self._row(23300, "PE", score=0)
        row.update(score=0, max_score=0, passed_filter_count=0, required_filter_count=8)
        with self.Session() as db:
            self._store(db, run_id=10, rows=[row], result_id=100)
            stored = db.scalar(select(S9FilterResult))
            self.assertEqual((stored.score, stored.max_score, stored.passed_count, stored.total_filters), (0, 0, 0, 8))

    def test_read_response_excludes_legacy_filter_per_row_records(self) -> None:
        result = ScreenerResult(
            id=100,
            screener_code="S9",
            symbol="NIFTY 50",
            signal="BUY_CALL",
            confidence=0.75,
            payload={},
        )
        result.s9_filter_results = [
            S9FilterResult(id=1, run_id=10, symbol="NIFTY 50", strike=23300, option_type="CE", filter_name="delta", passed=True),
            S9FilterResult(id=2, run_id=10, symbol="NIFTY 50", strike=23300, option_type="CE", filter_name="strike", passed=False, score=75),
        ]

        response = self.service._serialize_screener_row(result)  # noqa: SLF001

        self.assertEqual(len(response["payload"]["stored_filter_rows"]), 1)
        self.assertEqual(response["payload"]["stored_filter_rows"][0]["id"], 2)


if __name__ == "__main__":
    unittest.main()
