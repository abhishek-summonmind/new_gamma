import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings, validate_market_data_credentials
from app.services.data_ingestion_service import DataIngestionService
from app.services.dhan_service import DhanService
from app.services.option_types import OptionChainSnapshot, OptionContract


def settings(mode="live", symbol="NIFTY 50", token="token"):
    return Settings(
        database_url="sqlite:///./test.db",
        market_data_mode=mode,
        underlying_symbol=symbol,
        nifty_index_symbol=symbol,
        groww_access_token=token,
        dhan=DhanSettings(provider="groww"),
    )


def chain(symbol="NIFTY 50"):
    return OptionChainSnapshot(
        underlying=symbol,
        spot_price=24000.0,
        atm_strike=24000,
        expiry_date=date(2026, 8, 25),
        snapshot_time=datetime(2026, 7, 28, 12, 0),
        contracts=(OptionContract("broker-ce", 24000, "CE", 100, 10, 1, 20, delta=.5, iv=12.5),),
        requested_expiry=date(2026, 8, 25),
    )


class FakeClient:
    configured = True

    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.calls = []

    def get_option_chain(self, symbol, *, depth=None):
        self.calls.append((symbol, depth))
        if self.error:
            raise self.error
        return self.snapshot


class TestGrowwLiveMode(unittest.TestCase):
    def test_market_data_mode_is_authoritative_over_legacy_mock_flag(self):
        value = Settings(
            database_url="sqlite:///./test.db",
            market_data_mode="live",
            use_mock_data=True,
            groww_access_token="token",
            dhan=DhanSettings(provider="groww"),
        )
        self.assertFalse(value.use_mock_data)

    def test_mock_mode_never_calls_groww(self):
        service = DataIngestionService(settings("mock"))
        service._dhan = Mock()
        service._ingest_mock = Mock(return_value="mock-result")
        result = service.ingest(db=Mock(), run_id=1, now_market=datetime(2026, 7, 28, 12, 0))
        self.assertEqual(result, "mock-result")
        service._dhan.get_option_chain.assert_not_called()

    def test_invalid_live_credentials_return_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "Groww credential configuration is incomplete"):
            validate_market_data_credentials(settings("live", token=""))

    def test_live_failure_does_not_generate_mock_contracts(self):
        service = DataIngestionService(settings("live"))
        service._symbol_security_map = {}
        service._dhan = FakeClient(error=RuntimeError("invalid Groww token"))
        with self.assertRaisesRegex(RuntimeError, "Live option-chain fetch failed.*invalid Groww token"):
            service.ingest(db=Mock(), run_id=1, now_market=datetime(2026, 7, 28, 12, 0))

    def test_transient_chain_failure_can_use_recent_database_snapshot(self):
        service = DataIngestionService(settings("live"))
        now = datetime(2026, 7, 28, 12, 0)
        snapshot_time = now - timedelta(minutes=2)
        rows = [
            SimpleNamespace(
                security_id="cached-ce",
                underlying="NIFTY 50",
                expiry_date=date(2026, 8, 25),
                snapshot_time=snapshot_time,
                strike_price=24000,
                option_type="CE",
                ltp=100,
                oi=1000,
                oi_change=2,
                traded_volume=50,
            ),
            SimpleNamespace(
                security_id="cached-pe",
                underlying="NIFTY 50",
                expiry_date=date(2026, 8, 25),
                snapshot_time=snapshot_time,
                strike_price=24000,
                option_type="PE",
                ltp=110,
                oi=1200,
                oi_change=3,
                traded_volume=60,
            ),
        ]
        db = Mock()
        db.scalar.return_value = rows[0]
        db.scalars.return_value = rows

        cached = service._fallback_cached_option_chain(
            db=db,
            now_market=now,
            error="HTTP 404 GA000 underlying not found",
            frames_by_symbol={},
            expected_identity=("NSE", "NIFTY", date(2026, 8, 25)),
        )

        self.assertIsNotNone(cached)
        self.assertTrue(cached.fallback_used)
        self.assertEqual(len(cached.contracts), 2)

    def test_auth_failure_never_uses_cached_option_chain(self):
        service = DataIngestionService(settings("live"))
        db = Mock()

        cached = service._fallback_cached_option_chain(
            db=db,
            now_market=datetime(2026, 7, 28, 12, 0),
            error="invalid Groww token",
            frames_by_symbol={},
        )

        self.assertIsNone(cached)
        db.scalar.assert_not_called()

    def test_every_refresh_ingestion_calls_live_groww_chain(self):
        service = DataIngestionService(settings("live"))
        service._symbol_security_map = {}
        client = FakeClient(snapshot=chain())
        service._dhan = client
        service._load_previous_option_map = Mock(return_value={})
        service._store_option_chain = Mock()
        for run_id in (1, 2):
            result = service.ingest(db=Mock(), run_id=run_id, now_market=datetime(2026, 7, 28, 12, 0))
            self.assertIs(result.option_chain, client.snapshot)
        self.assertEqual(client.calls, [("NIFTY 50", 9), ("NIFTY 50", 9)])

    def test_supported_indices_have_isolated_groww_mappings(self):
        expected = {
            "NIFTY 50": ("NIFTY", "NSE", "NSE-NIFTY"),
            "FINNIFTY": ("FINNIFTY", "NSE", "NSE-NIFTY_FIN_SERVICE"),
            "SENSEX": ("SENSEX", "BSE", "BSE-SENSEX"),
        }
        for symbol, values in expected.items():
            service = DhanService(settings(symbol=symbol))
            context = service._groww_index_context(symbol)
            self.assertEqual((context["underlying"], context["exchange"], context["equity"]), values)
            self.assertEqual(service._groww_underlying_symbol(symbol), values[0])
            self.assertEqual(service._groww_equity_symbol(symbol), values[2])


if __name__ == "__main__":
    unittest.main()
