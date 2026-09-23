import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.api.routes import _serialize_dry_run_trade  # noqa: E402
from app.models import AutoTrade  # noqa: E402


class TestDryRunTradeSerializer(unittest.TestCase):
    def test_returns_only_fields_needed_by_minimal_table(self) -> None:
        entry_time = datetime(2026, 9, 22, 10, 5, 30)
        exit_time = datetime(2026, 9, 22, 10, 20, 0)
        trade = AutoTrade(
            id=7,
            symbol="NIFTY 50",
            option_symbol="NIFTY26SEP25000CE",
            expiry_date=date(2026, 9, 24),
            strike=25000,
            option_type="CE",
            direction="BULLISH",
            score=91,
            status="CLOSED",
            dry_run=True,
            initial_quantity=75,
            filled_quantity=75,
            open_quantity=0,
            average_entry_price=112.5,
            hard_stop_price=101.25,
            current_ltp=119.8,
            unrealized_pnl=0,
            unrealized_pnl_pct=0,
            entry_filled_at=entry_time,
            exit_time=exit_time,
            exit_price=119.8,
            exit_reason="TARGET_EXIT",
            details={"eligibility_snapshot": {"passed_count": 7, "total_filters": 8}},
        )

        payload = _serialize_dry_run_trade(trade)

        self.assertEqual(
            set(payload),
            {
                "id",
                "instrument",
                "strike",
                "type",
                "entry_time",
                "entry_price",
                "entry_score",
                "entry_pass",
                "ltp",
                "sl",
                "status",
                "exit_time",
                "exit_price",
                "exit_reason",
            },
        )
        self.assertEqual(payload["instrument"], "NIFTY 50")
        self.assertEqual(payload["entry_time"], entry_time.isoformat())
        self.assertEqual(payload["entry_score"], 91)
        self.assertEqual(payload["entry_pass"], "7/8")
        self.assertNotIn("quantity", payload)
        self.assertNotIn("expiry", payload)
        self.assertNotIn("pnl", payload)
        self.assertNotIn("pnl_percentage", payload)


if __name__ == "__main__":
    unittest.main()
