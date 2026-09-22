import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings
from app.services.dhan_service import DhanService
from app.services.indicator_engine import IndicatorEngine


class Response:
    status_code = 200
    reason = "OK"

    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


class HistoricalSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append((url, dict(params)))
        return Response(self.payloads.pop(0))


class InstrumentSession:
    def __init__(self, csv_text):
        self.csv_text = csv_text
        self.calls = 0
        self.lock = threading.Lock()

    def get(self, url, params, headers, timeout):
        with self.lock:
            self.calls += 1
        return Response(text=self.csv_text)


def settings(symbol):
    return Settings(
        database_url="sqlite:///./test.db", market_data_mode="live",
        underlying_symbol=symbol, nifty_index_symbol=symbol,
        groww_access_token="token", dhan=DhanSettings(provider="groww", timeout_seconds=1),
    )


def list_candles(count=40):
    start = datetime(2026, 7, 28, 9, 15)
    return [[(start + timedelta(minutes=5*i)).isoformat(), 100+i, 101+i, 99+i, 100.5+i, 1000+i] for i in range(count)]


class TestGrowwOhlcAndCache(unittest.TestCase):
    def fetch(self, symbol, payloads):
        service = DhanService(settings(symbol))
        service._session = HistoricalSession(payloads)
        frame = service.get_groww_intraday_ohlc(
            trading_symbol=symbol, from_date=datetime(2026, 7, 28, 9, 15),
            to_date=datetime(2026, 7, 28, 12, 30), interval_minutes=5,
        )
        return service, frame

    def test_nifty_50_ohlc_success_keeps_primary_mapping(self):
        service, frame = self.fetch("NIFTY 50", [{"status": "SUCCESS", "payload": {"candles": list_candles()}}])
        _, params = service._session.calls[0]
        self.assertEqual((params["exchange"], params["segment"], params["groww_symbol"]), ("NSE", "CASH", "NSE-NIFTY"))
        self.assertEqual(len(frame), 40)

    def test_finnifty_ohlc_uses_dynamic_alternate_symbol(self):
        service, frame = self.fetch("FINNIFTY", [
            {"status": "SUCCESS", "payload": {"candles": []}},
            {"status": "SUCCESS", "payload": {"candles": list_candles(35)}},
        ])
        symbols = [call[1]["groww_symbol"] for call in service._session.calls]
        self.assertEqual(symbols, ["NSE-NIFTY_FIN_SERVICE", "NSE-FINNIFTY"])
        self.assertEqual(len(frame), 35)

    def test_sensex_ohlc_uses_bse_exchange(self):
        service, frame = self.fetch("SENSEX", [{"status": "SUCCESS", "payload": {"candles": list_candles(36)}}])
        _, params = service._session.calls[0]
        self.assertEqual((params["exchange"], params["segment"], params["groww_symbol"]), ("BSE", "CASH", "BSE-SENSEX"))
        self.assertEqual(len(frame), 36)

    def test_http_200_empty_payload_returns_structured_error(self):
        service = DhanService(settings("SENSEX"))
        service._session = HistoricalSession([
            {"status": "SUCCESS", "payload": {"candles": []}},
            {"status": "SUCCESS", "payload": {"candles": []}},
        ])
        with self.assertRaisesRegex(RuntimeError, "symbol=SENSEX exchange=BSE segment=CASH.*attempts="):
            service.get_groww_intraday_ohlc(
                trading_symbol="SENSEX", from_date=datetime(2026, 7, 28, 9, 15),
                to_date=datetime(2026, 7, 28, 12, 30), interval_minutes=5,
            )

    def test_alternate_payload_and_dict_candle_structure(self):
        rows = [{"time": "2026-07-28T09:15:00", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 123}]
        _, frame = self.fetch("NIFTY 50", [{"status": "SUCCESS", "data": {"candleData": rows}}])
        self.assertEqual(len(frame), 1)
        self.assertEqual(float(frame.iloc[0]["close"]), 10.5)

    def test_valid_candles_enable_indicator_computation(self):
        _, frame = self.fetch("FINNIFTY", [
            {"status": "SUCCESS", "payload": {"candles": []}},
            {"status": "SUCCESS", "payload": {"candles": list_candles(45)}},
        ])
        db = Mock()
        result = IndicatorEngine(settings("FINNIFTY")).compute_and_store(
            db=db, run_id=1, frames_by_symbol={"FINNIFTY": frame},
            now_market=datetime(2026, 7, 28, 13, 0),
        )
        self.assertTrue(any(bucket.get("FINNIFTY") for bucket in result.states_by_timeframe.values()))

    def test_multi_day_lookback_is_not_truncated_to_current_session(self):
        service = DhanService(settings("NIFTY 50"))
        start, end = service._normalize_groww_session_range(
            datetime(2026, 7, 27, 12, 0),
            datetime(2026, 7, 28, 12, 30),
        )

        self.assertEqual(start.isoformat(), "2026-07-27T09:15:00+05:30")
        self.assertEqual(end.isoformat(), "2026-07-28T12:30:00+05:30")
        self.assertLess(start.date(), end.date())

    def test_empty_cache_is_regenerated_atomically(self):
        csv_text = "underlying_symbol,segment,expiry_date,extra\nNIFTY,FNO,2026-08-25," + ("x" * 80) + "\n"
        service = DhanService(settings("NIFTY 50"))
        service._session = InstrumentSession(csv_text)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "master.csv")
            Path(path).write_text("", encoding="utf-8")
            frame = service._load_or_refresh_groww_instrument_master(path)
            self.assertEqual(len(frame), 1)
            self.assertGreater(os.path.getsize(path), 64)
            self.assertEqual(service._session.calls, 1)

    def test_corrupt_cache_is_regenerated(self):
        csv_text = "underlying_symbol,segment,expiry_date,extra\nSENSEX,FNO,2026-08-27," + ("x" * 80) + "\n"
        service = DhanService(settings("SENSEX"))
        service._session = InstrumentSession(csv_text)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "master.csv")
            Path(path).write_text("bad_column\n" + ("x" * 80), encoding="utf-8")
            frame = service._load_or_refresh_groww_instrument_master(path)
            self.assertEqual(len(frame), 1)
            self.assertEqual(service._session.calls, 1)

    def test_concurrent_cache_refresh_downloads_once(self):
        csv_text = "underlying_symbol,segment,expiry_date,extra\nFINNIFTY,FNO,2026-08-25," + ("x" * 80) + "\n"
        service = DhanService(settings("FINNIFTY"))
        service._session = InstrumentSession(csv_text)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "master.csv")
            def load():
                with __import__("app.services.dhan_service", fromlist=["_INSTRUMENT_CACHE_THREAD_LOCK"])._INSTRUMENT_CACHE_THREAD_LOCK:
                    return service._load_or_refresh_groww_instrument_master(path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                frames = list(pool.map(lambda _: load(), range(4)))
            self.assertTrue(all(len(frame) == 1 for frame in frames))
            self.assertEqual(service._session.calls, 1)


if __name__ == "__main__":
    unittest.main()

