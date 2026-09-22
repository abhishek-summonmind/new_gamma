import os
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, PendingRollbackError

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings
from app.db import SessionLocal, engine, init_db
from app.models import MarketCandle, RefreshRun
from app.services.data_ingestion_service import DataIngestionService


def frame(close=100.5, rows=3):
    index = pd.date_range("2026-07-28 03:45:00+00:00", periods=rows, freq="1min")
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [close] * rows,
            "volume": [1000.0] * rows,
        },
        index=index,
    )


@unittest.skipUnless(engine.dialect.name == "postgresql", "PostgreSQL UPSERT integration test")
class TestMarketCandlePostgresUpsert(unittest.TestCase):
    def setUp(self):
        init_db()
        self.symbol = f"UPSERT-{uuid.uuid4().hex[:12]}"
        self.trigger = f"upsert-{uuid.uuid4().hex[:12]}"
        self.run_ids = []
        self.service = DataIngestionService(Settings(database_url=str(engine.url)))

    def tearDown(self):
        with SessionLocal() as db:
            db.execute(delete(MarketCandle).where(MarketCandle.symbol == self.symbol))
            if self.run_ids:
                db.execute(delete(RefreshRun).where(RefreshRun.id.in_(self.run_ids)))
            db.commit()

    def new_run(self):
        with SessionLocal() as db:
            run = RefreshRun(trigger=self.trigger, status="running", started_at=datetime.utcnow())
            db.add(run)
            db.commit()
            db.refresh(run)
            self.run_ids.append(run.id)
            return run.id

    def write(self, run_id, data, barrier=None):
        with SessionLocal() as db:
            if barrier:
                barrier.wait()
            summary = self.service._replace_candles(
                db=db, run_id=run_id, symbol=self.symbol,
                security_id="TEST", frame=data,
            )
            db.commit()
            return summary

    def test_duplicate_refresh_is_idempotent(self):
        first = self.write(self.new_run(), frame())
        second = self.write(self.new_run(), frame())
        self.assertEqual(first, {"inserted": 3, "updated": 0, "skipped": 0})
        self.assertEqual(second, {"inserted": 0, "updated": 3, "skipped": 0})
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(MarketCandle).where(MarketCandle.symbol == self.symbol))
        self.assertEqual(count, 3)

    def test_upsert_updates_values_and_run_but_preserves_created_at(self):
        first_run = self.new_run()
        self.write(first_run, frame(close=100.5, rows=1))
        with SessionLocal() as db:
            original = db.scalar(select(MarketCandle).where(MarketCandle.symbol == self.symbol))
            created_at = original.created_at
        second_run = self.new_run()
        summary = self.write(second_run, frame(close=111.25, rows=1))
        with SessionLocal() as db:
            updated = db.scalar(select(MarketCandle).where(MarketCandle.symbol == self.symbol))
            self.assertEqual(updated.close_price, 111.25)
            self.assertEqual(updated.run_id, second_run)
            self.assertEqual(updated.created_at, created_at)
        self.assertEqual(summary["updated"], 1)

    def test_concurrent_refreshes_are_safe(self):
        run_ids = [self.new_run(), self.new_run()]
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.write, run_id, frame(close=100 + i), barrier) for i, run_id in enumerate(run_ids)]
            summaries = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(item["inserted"] for item in summaries), 3)
        self.assertEqual(sum(item["updated"] for item in summaries), 3)
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(MarketCandle).where(MarketCandle.symbol == self.symbol))
        self.assertEqual(count, 3)

    def test_repeated_scheduler_execution_100_times(self):
        totals = {"inserted": 0, "updated": 0, "skipped": 0}
        for _ in range(100):
            summary = self.write(self.new_run(), frame(rows=1))
            for key in totals:
                totals[key] += summary[key]
        self.assertEqual(totals, {"inserted": 1, "updated": 99, "skipped": 0})

    def test_failed_statement_rolls_back_and_session_recovers(self):
        valid_run = self.new_run()
        with SessionLocal() as db:
            missing_run_id = 2_147_483_647
            with self.assertRaises(IntegrityError):
                self.service._replace_candles(
                    db=db, run_id=missing_run_id, symbol=self.symbol,
                    security_id="TEST", frame=frame(rows=1),
                )
            # The same Session must be immediately reusable; PendingRollbackError
            # here would prove rollback recovery is broken.
            try:
                summary = self.service._replace_candles(
                    db=db, run_id=valid_run, symbol=self.symbol,
                    security_id="TEST", frame=frame(rows=1),
                )
                db.commit()
            except PendingRollbackError as exc:
                self.fail(f"Session remained in PendingRollback state: {exc}")
        self.assertEqual(summary["inserted"], 1)


class TestMarketCandleUpsertStatement(unittest.TestCase):
    def test_input_duplicate_timestamps_are_skipped_before_upsert(self):
        service = DataIngestionService(Settings(database_url="sqlite:///./test.db"))
        duplicated = pd.concat([frame(rows=1), frame(close=105, rows=1)])

        class Scalars:
            def scalars(self):
                return iter([True])

        class FakeSession:
            def __init__(self):
                self.statement = None
                self.rollback_called = False
            def execute(self, statement):
                self.statement = statement
                return Scalars()
            def rollback(self):
                self.rollback_called = True

        db = FakeSession()
        summary = service._replace_candles(
            db=db, run_id=1, symbol="NIFTY 50", security_id="13", frame=duplicated,
        )
        compiled = str(db.statement.compile(dialect=engine.dialect)).upper()
        self.assertIn("ON CONFLICT", compiled)
        self.assertIn("DO UPDATE", compiled)
        self.assertNotIn("CREATED_AT = EXCLUDED.CREATED_AT", compiled)
        self.assertEqual(summary, {"inserted": 1, "updated": 0, "skipped": 1})


class TestIncrementalCandleFetch(unittest.TestCase):
    def setUp(self):
        self.service = DataIngestionService(
            Settings(database_url="sqlite:///./test.db", lookback_candles=80)
        )
        self.requested_windows = []

        class FakeClient:
            def __init__(inner_self, windows):
                inner_self.windows = windows

            def get_intraday_ohlc(inner_self, **kwargs):
                inner_self.windows.append((kwargs["from_date"], kwargs["to_date"]))
                return frame(rows=1)

        self.client = FakeClient(self.requested_windows)
        self.service._dhan = self.client

    def test_sufficient_history_fetches_only_incremental_overlap(self):
        latest = datetime(2026, 7, 28, 9, 45)
        db = Mock()
        db.scalar.side_effect = [latest, 500]
        now = datetime(2026, 7, 28, 9, 47, tzinfo=timezone.utc)
        history_start = now - timedelta(days=5)

        self.service._fetch_symbol_frame(
            db=db,
            symbol="NIFTY 50",
            security_id="13",
            fetch_from=history_start,
            now_market=now,
        )

        request_from, _ = self.requested_windows[0]
        self.assertEqual(
            request_from,
            latest.replace(tzinfo=ZoneInfo(self.service._settings.market_timezone)) - timedelta(minutes=1),
        )
        self.assertNotEqual(request_from, history_start)

    def test_missing_history_keeps_full_bootstrap_window(self):
        db = Mock()
        db.scalar.side_effect = [None, 0]
        now = datetime(2026, 7, 28, 9, 47, tzinfo=timezone.utc)
        history_start = now - timedelta(days=5)

        self.service._fetch_symbol_frame(
            db=db,
            symbol="NIFTY 50",
            security_id="13",
            fetch_from=history_start,
            now_market=now,
        )

        request_from, _ = self.requested_windows[0]
        self.assertEqual(request_from, history_start)


if __name__ == "__main__":
    unittest.main()

