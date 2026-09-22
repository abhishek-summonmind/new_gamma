import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import PropertyMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import DhanSettings, Settings
from app.services.dhan_client import DhanClient
from app.services.dhan_service import DhanService


class TestDhanClientSettingsOwnership(unittest.TestCase):
    def test_get_nifty_option_chain_uses_global_settings_underlying_symbol(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            underlying_symbol="DIXON",
            nifty_index_symbol="DIXON",
            nifty_symbols=("DIXON",),
            dhan=DhanSettings(
                access_token="test-token",
                client_id="test-client",
            ),
        )

        client = DhanClient(settings)

        with patch.object(type(client._service), "configured", new_callable=PropertyMock, return_value=True), \
            patch.object(client, "_get_expiry_dates", return_value=[date.today()]), \
            patch.object(
                client,
                "_get_option_chain",
                return_value={
                    "last_price": 500.0,
                    "oc": {
                        "100": {
                            "ce": {
                                "security_id": "111",
                                "last_price": 10.0,
                                "open_interest": 100,
                                "oi_change": 1,
                                "volume": 5,
                            },
                            "pe": {
                                "security_id": "222",
                                "last_price": 11.0,
                                "open_interest": 120,
                                "oi_change": 2,
                                "volume": 6,
                            },
                        },
                    },
                },
            ):
            snapshot = client.get_nifty_option_chain(depth=0)

        self.assertEqual(snapshot.underlying, "DIXON")
        self.assertEqual(snapshot.atm_strike, 100)

    def test_default_option_chain_depth_scans_two_strikes_each_side(self) -> None:
        settings = DhanSettings()
        strikes = [float(strike) for strike in range(55000, 57001, 50)]

        selected = DhanClient._slice_strikes_around_atm(
            strikes,
            atm_strike=56000,
            depth=settings.option_chain_depth,
        )

        self.assertEqual(settings.option_chain_depth, 2)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0], 55900.0)
        self.assertEqual(selected[-1], 56100.0)

    def test_dixon_option_chain_uses_equity_underlying_segment(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            underlying_symbol="DIXON",
            nifty_index_symbol="DIXON",
            nifty_symbols=("DIXON",),
            dhan=DhanSettings(
                access_token="test-token",
                client_id="test-client",
                nifty_security_id="21690",
                equity_exchange_segment="NSE_EQ",
                options_exchange_segment="NSE_FNO",
            ),
        )

        service = DhanService(settings=settings)

        self.assertEqual(service._resolve_option_chain_context("DIXON"), ("21690", "NSE_EQ"))


if __name__ == "__main__":
    unittest.main()
