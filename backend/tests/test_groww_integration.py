import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings  # noqa: E402
from app.services.dhan_service import DhanService  # noqa: E402


class _Response:
    status_code = 200
    text = ""
    reason = "OK"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params, headers, timeout):
        safe_headers = {key: ("***" if key == "Authorization" else value) for key, value in headers.items()}
        self.calls.append((url, params, safe_headers))
        if "historical/candles" in url:
            return _Response({"status": "SUCCESS", "payload": {"candles": [["2026-07-27T09:15:00", 1, 2, 0.5, 1.5, 100]]}})
        return _Response(
            {
                "status": "SUCCESS",
                "payload": {
                    "underlying_ltp": 14000,
                    "strikes": {
                        "14000": {
                            "CE": {"trading_symbol": "DIXON25AUG14000CE", "ltp": 10, "open_interest": 1, "volume": 2},
                            "PE": {"trading_symbol": "DIXON25AUG14000PE", "ltp": 9, "open_interest": 1, "volume": 2},
                        }
                    },
                },
            }
        )

    def post(self, url, json, headers, timeout):
        self.calls.append((url, json, headers))
        return _Response({"token": "generated-groww-access-token"})


class _GrowwService(DhanService):
    def __init__(self, expiries):
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_timezone="Asia/Kolkata",
            dhan=DhanSettings(
                provider="groww",
                groww_access_token="token",
                groww_equity_symbol="NSE-DIXON",
                groww_trading_symbol="DIXON",
                groww_underlying_symbol="DIXON",
                option_expiry=date(2026, 8, 25),
            ),
        )
        super().__init__(settings=settings)
        self._session = _Session()
        self._expiries = set(expiries)
        self._config_service.get_option_expiry = lambda: date(2026, 8, 25)

    def _get_groww_fno_expiries(self, underlying: str) -> set[date]:
        return set(self._expiries)


class TestGrowwIntegration(unittest.TestCase):
    def test_placeholder_groww_api_key_is_not_treated_as_configured(self):
        settings = DhanSettings(provider="groww", groww_access_token="your_groww_api_key")
        service = DhanService(dhan_settings=settings)

        self.assertFalse(service.configured)

    def test_groww_never_falls_back_to_dhan_database_token(self):
        service = _GrowwService({date(2026, 8, 25)})
        service._settings.groww_access_token = ""
        service._dhan.groww_access_token = ""
        service._config_service.get_effective_access_token = lambda: "dhan-db-token"

        self.assertFalse(service.configured)
        self.assertEqual(service._effective_access_token(), "")

    def test_api_key_and_secret_generate_and_cache_access_token(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            dhan=DhanSettings(
                provider="groww",
                groww_api_key="groww-api-key",
                groww_api_secret="groww-api-secret",
            ),
        )
        service = DhanService(settings=settings)
        service._session = _Session()

        self.assertTrue(service.configured)
        self.assertEqual(service._effective_access_token(), "generated-groww-access-token")
        self.assertEqual(service._effective_access_token(), "generated-groww-access-token")
        self.assertEqual(len(service._session.calls), 1)
        url, payload, headers = service._session.calls[0]
        self.assertTrue(url.endswith("/token/api/access"))
        self.assertEqual(payload["key_type"], "approval")
        self.assertEqual(len(payload["checksum"]), 64)
        self.assertEqual(headers["Authorization"], "Bearer groww-api-key")

    def test_direct_groww_access_token_takes_priority(self):
        settings = DhanSettings(
            provider="groww",
            groww_api_key="groww-api-key",
            groww_api_secret="groww-api-secret",
            groww_access_token="direct-groww-access-token",
        )
        service = DhanService(dhan_settings=settings)
        service._session = _Session()

        self.assertEqual(service._effective_access_token(), "direct-groww-access-token")
        self.assertEqual(service._session.calls, [])

    def test_historical_uses_candles_endpoint_and_groww_symbol(self):
        service = _GrowwService({date(2026, 8, 25)})
        frame = service.get_groww_intraday_ohlc(
            trading_symbol="DIXON",
            from_date=datetime(2026, 7, 27, 8, 0),
            to_date=datetime(2026, 7, 27, 16, 0),
            interval_minutes=1,
        )

        url, params, headers = service._session.calls[0]
        self.assertTrue(url.endswith("/historical/candles"))
        self.assertEqual(params["groww_symbol"], "NSE-DIXON")
        self.assertNotIn("trading_symbol", params)
        self.assertEqual(params["candle_interval"], "1minute")
        self.assertNotIn("interval_in_minutes", params)
        self.assertEqual(params["start_time"], "2026-07-27 09:15:00")
        self.assertEqual(params["end_time"], "2026-07-27 15:30:00")
        self.assertEqual(headers["X-API-VERSION"], "1.0")
        self.assertEqual(len(frame), 1)

    def test_option_chain_falls_back_to_nearest_available_expiry(self):
        service = _GrowwService({date(2026, 8, 28)})
        chain = service.get_option_chain("DIXON")

        _, params, _ = service._session.calls[0]
        self.assertEqual(params, {"expiry_date": "2026-08-28"})
        self.assertEqual(chain.expiry_date, date(2026, 8, 28))

    def test_option_chain_sends_request_for_valid_expiry(self):
        service = _GrowwService({date(2026, 8, 25)})
        chain = service.get_option_chain("DIXON")

        url, params, headers = service._session.calls[0]
        self.assertTrue(url.endswith("/option-chain/exchange/NSE/underlying/DIXON"))
        self.assertEqual(params, {"expiry_date": "2026-08-25"})
        self.assertEqual(headers["Authorization"], "***")
        self.assertEqual(chain.atm_strike, 14000)
        self.assertEqual(len(chain.contracts), 2)

    def test_instrument_master_malformed_csv_returns_clear_error(self):
        service = _GrowwService({date(2026, 8, 25)})

        class _BadCsvSession(_Session):
            def get(self, url, params, headers, timeout):
                self.calls.append((url, params, headers))
                return type("BadCsvResponse", (), {"status_code": 200, "text": "bad_column\nDIXON\n"})()

        service._session = _BadCsvSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = str(Path(temp_dir) / "groww_instrument_master.csv")
            with self.assertRaisesRegex(RuntimeError, "Groww instrument master missing required columns"):
                service._load_or_refresh_groww_instrument_master(cache_path)

if __name__ == "__main__":
    unittest.main()





