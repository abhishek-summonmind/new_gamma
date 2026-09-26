import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.dhan_service import DhanService  # noqa: E402


class TestOptionExpiryFallback(unittest.TestCase):
    def test_exact_non_expired_expiry_is_preserved(self) -> None:
        configured = date(2026, 8, 6)
        resolved = DhanService._nearest_available_expiry(
            configured_expiry=configured,
            available_expiries={date(2026, 7, 30), configured},
            today=date(2026, 7, 28),
        )
        self.assertEqual(resolved, configured)

    def test_unavailable_expiry_uses_nearest_future_expiry(self) -> None:
        resolved = DhanService._nearest_available_expiry(
            configured_expiry=date(2026, 8, 5),
            available_expiries={date(2026, 7, 30), date(2026, 8, 6), date(2026, 8, 13)},
            today=date(2026, 7, 28),
        )
        self.assertEqual(resolved, date(2026, 8, 6))

    def test_tie_prefers_earlier_valid_expiry(self) -> None:
        resolved = DhanService._nearest_available_expiry(
            configured_expiry=date(2026, 8, 6),
            available_expiries={date(2026, 8, 5), date(2026, 8, 7)},
            today=date(2026, 7, 28),
        )
        self.assertEqual(resolved, date(2026, 8, 5))

    def test_expired_expiries_are_not_selected(self) -> None:
        resolved = DhanService._nearest_available_expiry(
            configured_expiry=date(2026, 7, 20),
            available_expiries={date(2026, 7, 20), date(2026, 7, 30)},
            today=date(2026, 7, 28),
        )
        self.assertEqual(resolved, date(2026, 7, 30))

    def test_clear_error_when_no_non_expired_expiry_exists(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No non-expired option expiry"):
            DhanService._nearest_available_expiry(
                configured_expiry=date(2026, 7, 28),
                available_expiries={date(2026, 7, 20)},
                today=date(2026, 7, 28),
            )

    def test_groww_validation_returns_resolved_expiry(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        configured = date.today().replace(day=min(date.today().day, 27))
        nearest = date.today()
        if nearest == configured:
            nearest = date.fromordinal(nearest.toordinal() + 1)

        with patch.object(service, "_get_groww_fno_expiries", return_value={nearest}):
            resolved = service._validate_groww_option_expiry(
                underlying="NIFTY",
                expiry=configured,
            )

        self.assertEqual(resolved, nearest)

    def test_stock_expiry_uses_next_month_when_current_has_under_14_days(self) -> None:
        expiries = {
            date(2026, 9, 24),
            date(2026, 10, 29),
        }
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )

        with patch.object(service, "_get_groww_fno_expiries", return_value=expiries):
            before_16th = service.resolve_groww_stock_option_expiry(
                underlying="DIXON",
                today=date(2026, 9, 15),
            )
            after_16th = service.resolve_groww_stock_option_expiry(
                underlying="DIXON",
                today=date(2026, 9, 17),
            )

        self.assertEqual(before_16th, date(2026, 10, 29))
        self.assertEqual(after_16th, date(2026, 10, 29))

    def test_stock_expiry_uses_next_month_after_current_expiry(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        with patch.object(
            service,
            "_get_groww_fno_expiries",
            return_value={date(2026, 9, 24), date(2026, 10, 29)},
        ):
            resolved = service.resolve_groww_stock_option_expiry(
                underlying="DIXON",
                today=date(2026, 9, 25),
            )
        self.assertEqual(resolved, date(2026, 10, 29))

    def test_stock_expiry_does_not_roll_december_to_january_after_16th(self) -> None:
        year, month = DhanService._stock_expiry_target_month(date(2026, 12, 17))

        self.assertEqual((year, month), (2026, 12))
        self.assertEqual(
            DhanService._monthly_stock_expiry(
                underlying="DIXON",
                available_expiries={date(2026, 12, 31), date(2027, 1, 28)},
                year=year,
                month=month,
                today=date(2026, 12, 17),
            ),
            date(2026, 12, 31),
        )

    def test_bank_nifty_and_finnifty_use_current_month_monthly_expiry(self) -> None:
        self.assertTrue(DhanService._uses_current_month_monthly_expiry("BANK NIFTY"))
        self.assertTrue(DhanService._uses_current_month_monthly_expiry("FINNIFTY"))
        self.assertFalse(DhanService._uses_current_month_monthly_expiry("NIFTY 50"))
        self.assertFalse(DhanService._uses_current_month_monthly_expiry("SENSEX"))

    def test_groww_option_chain_retries_ga000_on_same_validated_expiry(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        expiry = date(2026, 10, 27)
        recovered = {"status": "SUCCESS", "payload": {"strikes": {}}}

        with (
            patch.object(
                service,
                "get_json",
                side_effect=[
                    RuntimeError(
                        "Groww request failed: HTTP 500 - GA000 An internal server error occurred"
                    ),
                    recovered,
                ],
            ) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
        ):
            result = service._get_groww_option_chain_with_retry(  # noqa: SLF001
                endpoint="/option-chain/exchange/NSE/underlying/SRF",
                target="SRF",
                underlying="SRF",
                expiry=expiry,
                identity_validated=True,
            )

        self.assertEqual(result, recovered)
        self.assertEqual(request.call_count, 2)
        request.assert_called_with(
            "/option-chain/exchange/NSE/underlying/SRF",
            params={"expiry_date": "2026-10-27"},
        )
        sleep.assert_called_once_with(0.5)

    def test_groww_option_chain_does_not_retry_non_transient_error(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        with (
            patch.object(service, "get_json", side_effect=RuntimeError("HTTP 400 invalid expiry")) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "invalid expiry"),
        ):
            service._get_groww_option_chain_with_retry(  # noqa: SLF001
                endpoint="/option-chain/exchange/NSE/underlying/SRF",
                target="SRF",
                underlying="SRF",
                expiry=date(2026, 10, 27),
            )

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_groww_option_chain_recovers_after_validated_404_ga000(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        recovered = {"status": "SUCCESS", "payload": {"strikes": {"24000": {}}}}
        with (
            patch.object(
                service,
                "get_json",
                side_effect=[
                    RuntimeError(
                        "Groww request failed: HTTP 404 - GA000 Underlying not found for exchange 'NSE'"
                    ),
                    recovered,
                ],
            ) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
            self.assertLogs("app.services.dhan_service", level="INFO") as logs,
        ):
            result = service._get_groww_option_chain_with_retry(  # noqa: SLF001
                endpoint="/option-chain/exchange/NSE/underlying/NIFTY",
                target="NIFTY 50",
                underlying="NIFTY",
                expiry=date(2026, 9, 22),
                identity_validated=True,
            )

        self.assertEqual(result, recovered)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertTrue(
            any("GROWW_RETRY_RECOVERED" in message for message in logs.output)
        )

    def test_groww_option_chain_does_not_retry_unvalidated_identity(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        with (
            patch.object(
                service,
                "get_json",
                side_effect=RuntimeError(
                    "Groww request failed: HTTP 404 - GA000 Underlying not found for exchange 'NSE'"
                ),
            ) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Underlying not found"),
        ):
            service._get_groww_option_chain_with_retry(  # noqa: SLF001
                endpoint="/option-chain/exchange/NSE/underlying/INVALID",
                target="INVALID",
                underlying="INVALID",
                expiry=date(2026, 9, 22),
                identity_validated=False,
            )

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_validated_404_ga000_exhausts_after_three_attempts_with_backoff(self) -> None:
        service = DhanService(
            settings=Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                use_mock_data=False,
            )
        )
        error = RuntimeError(
            "Groww request failed: HTTP 404 - GA000 Underlying not found for exchange 'NSE'"
        )
        with (
            patch.object(service, "get_json", side_effect=error) as request,
            patch("app.services.dhan_service.time_module.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Underlying not found"),
        ):
            service._get_groww_option_chain_with_retry(  # noqa: SLF001
                endpoint="/option-chain/exchange/NSE/underlying/NIFTY",
                target="NIFTY 50",
                underlying="NIFTY",
                expiry=date(2026, 9, 22),
                identity_validated=True,
                max_attempts=9,
            )

        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])


if __name__ == "__main__":
    unittest.main()
