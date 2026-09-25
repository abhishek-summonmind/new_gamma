from __future__ import annotations

import unittest

from backend.app.config import Settings
from backend.app.services.s9_ltp_stream_service import S9LTPStreamService


class _RefreshService:
    @staticmethod
    def get_s9_top_opportunities(*, limit: int) -> dict:
        assert limit == 6
        return {
            "items": [{
                "symbol": "NIFTY 50",
                "payload": {
                    "underlying_symbol": "NIFTY 50",
                    "selected_strike": 25000,
                    "selected_option_type": "CE",
                    "strike_scan": {"expiry": "2026-09-29"},
                    "filters": {"pcr": {"passed": True}},
                    "score": 85,
                },
            }],
        }


class _DhanService:
    @staticmethod
    def get_groww_access_token() -> str:
        return "test-token"

    @staticmethod
    def resolve_groww_option_instrument(**_kwargs) -> dict:
        return {
            "exchange": "NSE",
            "segment": "FNO",
            "security_id": "12345",
            "trading_symbol": "NIFTY26SEP25000CE",
        }


class _Feed:
    def __init__(self, _api) -> None:
        self.callback = None
        self.unsubscribed = False

    def subscribe_ltp(self, instruments, *, on_data_received) -> None:
        self.instruments = instruments
        self.callback = on_data_received

    def unsubscribe_ltp(self, _instruments) -> None:
        self.unsubscribed = True

    @staticmethod
    def get_ltp() -> dict:
        return {
            "NSE": {
                "FNO": {
                    "12345": {"ltp": 123.45, "tsInMillis": 1_790_000_000_000},
                },
            },
        }


class S9LTPStreamServiceTests(unittest.TestCase):
    def test_stream_emits_only_identity_ltp_and_timestamp(self) -> None:
        feed_holder = {}

        def feed_factory(api):
            feed_holder["feed"] = _Feed(api)
            return feed_holder["feed"]

        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            dhan={"provider": "groww", "groww_access_token": "test-token"},
        )
        rows = []
        service = S9LTPStreamService(
            settings,
            _RefreshService(),
            dhan_service=_DhanService(),
            api_factory=lambda token: {"token": token},
            feed_factory=feed_factory,
        )

        self.assertEqual(service.start(rows.append), 1)
        feed = feed_holder["feed"]
        feed.callback({"exchange": "NSE", "segment": "FNO", "feed_key": "12345"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "s9_ltp")
        self.assertEqual(rows[0]["ltp"], 123.45)
        self.assertEqual(rows[0]["strike"], 25000.0)
        self.assertNotIn("filters", rows[0])
        self.assertNotIn("score", rows[0])

        service.close()
        self.assertTrue(feed.unsubscribed)


if __name__ == "__main__":
    unittest.main()
