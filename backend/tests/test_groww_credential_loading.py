import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import (  # noqa: E402
    DhanSettings,
    Settings,
    _load_environment_files,
    get_settings,
    validate_live_market_data_connection,
    validate_market_data_credentials,
)


class TestGrowwCredentialLoading(unittest.TestCase):
    credential_names = (
        "MARKET_DATA_PROVIDER",
        "GROWW_ACCESS_TOKEN",
        "GROWW_API_KEY",
        "GROWW_API_SECRET",
    )

    def setUp(self):
        self.original = {name: os.environ.get(name) for name in self.credential_names}
        get_settings.cache_clear()

    def tearDown(self):
        for name, value in self.original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()

    def test_environment_credentials_map_to_active_settings(self):
        os.environ["MARKET_DATA_PROVIDER"] = "groww"
        os.environ["GROWW_ACCESS_TOKEN"] = "runtime-access-token"
        os.environ["GROWW_API_KEY"] = "runtime-api-key"
        os.environ["GROWW_API_SECRET"] = "runtime-api-secret"

        settings = get_settings()

        self.assertEqual(settings.groww_access_token, "runtime-access-token")
        self.assertEqual(settings.groww_api_key, "runtime-api-key")
        self.assertEqual(settings.groww_api_secret, "runtime-api-secret")
        self.assertEqual(settings.dhan.groww_access_token, "runtime-access-token")
        self.assertEqual(settings.dhan.groww_api_key, "runtime-api-key")
        self.assertEqual(settings.dhan.groww_api_secret, "runtime-api-secret")
        self.assertEqual(
            settings.groww_credential_status,
            {
                "access_token": True,
                "api_key": True,
                "api_secret": True,
                "auth_ready": True,
            },
        )

    def test_access_token_only_is_auth_ready(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            groww_access_token="access-token",
            dhan=DhanSettings(provider="groww"),
        )
        validate_market_data_credentials(settings)
        self.assertTrue(settings.groww_credential_status["auth_ready"])

    def test_api_key_and_secret_are_auth_ready(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            groww_api_key="api-key",
            groww_api_secret="api-secret",
            dhan=DhanSettings(provider="groww"),
        )
        validate_market_data_credentials(settings)
        self.assertTrue(settings.groww_credential_status["auth_ready"])

    def test_mock_mode_does_not_require_groww_credentials(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_data_mode="mock",
            dhan=DhanSettings(provider="groww"),
        )

        validate_market_data_credentials(settings)
        self.assertFalse(settings.groww_credential_status["auth_ready"])

    def test_get_settings_exposes_all_groww_fields(self):
        settings = get_settings()
        for name in (
            "groww_access_token", "groww_api_key", "groww_api_secret", "groww_base_url",
            "groww_api_version", "groww_exchange", "groww_cash_segment", "groww_fno_segment",
            "groww_equity_symbol", "groww_trading_symbol", "groww_underlying_symbol",
            "groww_option_expiry",
        ):
            self.assertTrue(hasattr(settings, name), name)
    def test_blank_process_values_do_not_mask_dotenv_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "GROWW_ACCESS_TOKEN=file-access-token\n"
                "GROWW_API_KEY=file-api-key\n"
                "GROWW_API_SECRET=file-api-secret\n",
                encoding="utf-8",
            )
            for name in self.credential_names[1:]:
                os.environ[name] = " "

            with patch("app.config._environment_file_candidates", return_value=(env_path,)):
                _load_environment_files()

            self.assertEqual(os.environ["GROWW_ACCESS_TOKEN"], "file-access-token")
            self.assertEqual(os.environ["GROWW_API_KEY"], "file-api-key")
            self.assertEqual(os.environ["GROWW_API_SECRET"], "file-api-secret")

    def test_incomplete_groww_configuration_has_clear_error(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            dhan=DhanSettings(provider="groww", groww_api_key="only-key"),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Set GROWW_ACCESS_TOKEN, or set both GROWW_API_KEY and GROWW_API_SECRET",
        ):
            validate_market_data_credentials(settings)

    def test_transient_groww_404_does_not_abort_application_startup(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_data_mode="live",
            underlying_symbol="NIFTY 50",
            groww_access_token="access-token",
            dhan=DhanSettings(provider="groww"),
        )
        error = RuntimeError(
            "Groww request failed: HTTP 404 - GA000 Underlying not found for exchange 'NSE'"
        )

        with patch(
            "app.services.dhan_config_service.DhanConfigService.apply_runtime_market_data_config",
            return_value=settings,
        ), patch("app.services.dhan_client.DhanClient.get_option_chain", side_effect=error):
            validate_live_market_data_connection(settings)

    def test_invalid_groww_token_still_aborts_application_startup(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_data_mode="live",
            underlying_symbol="NIFTY 50",
            groww_access_token="access-token",
            dhan=DhanSettings(provider="groww"),
        )

        with patch(
            "app.services.dhan_config_service.DhanConfigService.apply_runtime_market_data_config",
            return_value=settings,
        ), patch(
            "app.services.dhan_client.DhanClient.get_option_chain",
            side_effect=RuntimeError("HTTP 401 invalid token"),
        ):
            with self.assertRaisesRegex(RuntimeError, "startup validation failed"):
                validate_live_market_data_connection(settings)

    def test_windows_socket_failure_does_not_abort_application_startup(self):
        settings = Settings(
            database_url="sqlite:///./test.db",
            market_data_mode="live",
            underlying_symbol="NIFTY 50",
            groww_access_token="access-token",
            dhan=DhanSettings(provider="groww"),
        )
        error = RuntimeError(
            "HTTPSConnectionPool: Failed to establish a new connection: "
            "[WinError 10013] socket access forbidden by its access permissions"
        )

        with patch(
            "app.services.dhan_config_service.DhanConfigService.apply_runtime_market_data_config",
            return_value=settings,
        ), patch("app.services.dhan_client.DhanClient.get_option_chain", side_effect=error):
            validate_live_market_data_connection(settings)


if __name__ == "__main__":
    unittest.main()



