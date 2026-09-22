import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings  # noqa: E402
from app.services.dhan_service import DhanService  # noqa: E402


EXPIRY = date(2026, 9, 29)
TRANSIENT_404 = RuntimeError(
    "Groww request failed: HTTP 404 - GA000 Underlying not found"
)


def _settings(symbol: str = "NIFTY 50") -> Settings:
    return Settings(
        database_url="sqlite:///./test.db",
        underlying_symbol=symbol,
        nifty_index_symbol=symbol,
        groww_access_token="token",
        dhan=DhanSettings(provider="groww", option_chain_depth=2),
    )


def _chain_payload(symbol: str = "NIFTY") -> dict:
    return {
        "status": "SUCCESS",
        "payload": {
            "underlying_ltp": 24000,
            "strikes": {
                "24000": {
                    "CE": {
                        "trading_symbol": f"{symbol}CE",
                        "ltp": 100,
                        "open_interest": 10,
                        "volume": 5,
                    },
                    "PE": {
                        "trading_symbol": f"{symbol}PE",
                        "ltp": 110,
                        "open_interest": 12,
                        "volume": 6,
                    },
                }
            },
        },
    }


def _candle_payload() -> dict:
    return {
        "status": "SUCCESS",
        "payload": {
            "candles": [["2026-09-21T09:15:00", 100, 102, 99, 101, 50]]
        },
    }


class TestGrowwTransientFallbacks(unittest.TestCase):
    def test_404_retry_then_200_succeeds(self) -> None:
        service = DhanService(settings=_settings())
        with (
            patch.object(service, "_get_groww_fno_expiries", return_value={EXPIRY}),
            patch.object(service, "get_json", side_effect=[TRANSIENT_404, _chain_payload()]) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
            self.assertLogs("app.services.dhan_service", level="INFO") as logs,
        ):
            chain = service.get_option_chain("NIFTY 50")

        self.assertEqual(chain.expiry_date, EXPIRY)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertTrue(any("GROWW_RETRY_RECOVERED" in row for row in logs.output))

    def test_all_404_attempts_use_exact_last_good_chain(self) -> None:
        service = DhanService(settings=_settings())
        with patch.object(service, "_get_groww_fno_expiries", return_value={EXPIRY}):
            with patch.object(service, "get_json", return_value=_chain_payload()):
                original = service.get_option_chain("NIFTY 50")
            with (
                patch.object(service, "get_json", side_effect=TRANSIENT_404) as request,
                patch("app.services.dhan_service.time_module.sleep"),
                self.assertLogs("app.services.dhan_service", level="WARNING") as logs,
            ):
                cached = service.get_option_chain("NIFTY 50")

        self.assertEqual(request.call_count, 3)
        self.assertEqual(cached.contracts, original.contracts)
        self.assertTrue(cached.fallback_used)
        self.assertTrue(any("GROWW_CACHE_FALLBACK" in row for row in logs.output))

    def test_sensex_transient_ga000_recovers_on_bse(self) -> None:
        service = DhanService(settings=_settings("SENSEX"))
        with (
            patch.object(service, "_get_groww_fno_expiries", return_value={EXPIRY}),
            patch.object(service, "get_json", side_effect=[TRANSIENT_404, _chain_payload("SENSEX")]) as request,
            patch("app.services.dhan_service.time_module.sleep"),
        ):
            chain = service.get_option_chain("SENSEX")

        self.assertEqual(len(chain.contracts), 2)
        self.assertTrue(request.call_args_list[0].args[0].startswith("/option-chain/exchange/BSE/"))

    def test_historical_primary_404_uses_compatibility_endpoint(self) -> None:
        service = DhanService(settings=_settings())
        with patch.object(
            service,
            "get_json",
            side_effect=[RuntimeError("Groww request failed: HTTP 404"), _candle_payload()],
        ) as request:
            frame = service.get_groww_intraday_ohlc(
                trading_symbol="NIFTY 50",
                from_date=datetime(2026, 9, 21, 9, 15),
                to_date=datetime(2026, 9, 21, 15, 30),
                interval_minutes=1,
            )

        self.assertFalse(frame.empty)
        self.assertEqual(request.call_args_list[0].args[0], "/historical/candles")
        self.assertEqual(request.call_args_list[1].args[0], "/historical/candle/range")

    def test_both_historical_endpoints_temporarily_fail_use_exact_cache(self) -> None:
        service = DhanService(settings=_settings())
        arguments = {
            "trading_symbol": "NIFTY 50",
            "from_date": datetime(2026, 9, 21, 9, 15),
            "to_date": datetime(2026, 9, 21, 15, 30),
            "interval_minutes": 1,
        }
        with patch.object(service, "get_json", return_value=_candle_payload()):
            original = service.get_groww_intraday_ohlc(**arguments)
        with (
            patch.object(
                service,
                "get_json",
                side_effect=[
                    RuntimeError("Groww request failed: HTTP 404"),
                    RuntimeError("Groww request failed: HTTP 503 GA003"),
                ],
            ) as request,
            self.assertLogs("app.services.dhan_service", level="WARNING") as logs,
        ):
            cached = service.get_groww_intraday_ohlc(**arguments)

        self.assertEqual(request.call_count, 2)
        pd.testing.assert_frame_equal(cached, original)
        self.assertTrue(any("GROWW_CACHE_FALLBACK" in row for row in logs.output))

    def test_invalid_expiry_fails_closed_before_request(self) -> None:
        service = DhanService(settings=_settings())
        with (
            patch.object(service, "_get_groww_fno_expiries", return_value={EXPIRY}),
            patch.object(service, "get_json") as request,
            self.assertLogs("app.services.dhan_service", level="ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, "instrument master does not contain"),
        ):
            service._validate_groww_option_identity(
                exchange="NSE",
                underlying="NIFTY",
                expiry=date(2026, 10, 6),
            )

        request.assert_not_called()
        self.assertTrue(any("GROWW_INSTRUMENT_INVALID" in row for row in logs.output))

    def test_cache_never_crosses_symbol_expiry_or_timeframe(self) -> None:
        service = DhanService(settings=_settings())
        snapshot = service._groww_option_chain_cache
        self.assertEqual(snapshot, {})

        # Populate only NIFTY/EXPIRY, then prove adjacent identities miss.
        with (
            patch.object(service, "_get_groww_fno_expiries", return_value={EXPIRY}),
            patch.object(service, "get_json", return_value=_chain_payload()),
        ):
            service.get_option_chain("NIFTY 50")
        self.assertIsNone(service._cached_groww_option_chain(("BSE", "SENSEX", EXPIRY)))
        self.assertIsNone(
            service._cached_groww_option_chain(("NSE", "NIFTY", date(2026, 10, 6)))
        )

        frame = pd.DataFrame(
            {"open": [1], "high": [2], "low": [1], "close": [2], "volume": [1]},
            index=pd.to_datetime(["2026-09-21T09:15:00Z"]),
        )
        service._cache_groww_candles(("NSE", "NIFTY 50", 1), frame)
        self.assertIsNone(service._cached_groww_candles(("NSE", "SENSEX", 1)))
        self.assertIsNone(service._cached_groww_candles(("NSE", "NIFTY 50", 5)))


if __name__ == "__main__":
    unittest.main()
