import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.option_types import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.screener_engine import (  # noqa: E402
    S9_FILTER_WEIGHTS,
    S9_INDEX_VOLUME_VALIDATION_START_SECONDS,
    S9_VOLUME_VALIDATION_START_SECONDS,
    ScreenerEngine,
)


def premium_state(*, option_symbol: str = "FINNIFTY 25000 CE") -> SimpleNamespace:
    latest = SimpleNamespace(
        candle_time=datetime(2026, 9, 25, 9, 15),
        open=100.0,
        high=112.0,
        low=99.0,
        close=110.0,
        volume=300.0,
        volume_ma=100.0,
        volume_ma20=100.0,
        vwap=105.0,
        rsi=55.0,
    )
    return SimpleNamespace(symbol=option_symbol, timeframe="3m", latest=latest, previous=None)


def contract(option_type: str = "CE") -> OptionContract:
    return OptionContract(
        security_id="12345",
        strike=25000.0,
        option_type=option_type,
        ltp=106.0,
        oi=1000.0,
        oi_change=10.0,
        volume=300.0,
        delta=0.60 if option_type == "CE" else -0.60,
        theta=-0.01,
        bid_price=105.95,
        ask_price=105.99,
        bid_qty=56.0,
        ask_qty=44.0,
        vega=0.10,
        gamma=0.01,
    )


def chain(underlying: str, row: OptionContract) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying=underlying,
        spot_price=25000.0,
        atm_strike=25000,
        expiry_date=date(2026, 9, 29),
        snapshot_time=datetime(2026, 9, 25, 9, 16, 30),
        contracts=(row,),
    )


class TestS9IndexOptionPremiumFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ScreenerEngine(
            Settings(
                database_url="sqlite:///./test.db",
                redis_url="memory://",
                underlying_symbol="FINNIFTY",
                nifty_index_symbol="FINNIFTY",
            )
        )

    def tearDown(self) -> None:
        self.engine._cache._GLOBAL_MEMORY_CACHE.clear()

    def test_stock_bank_finnifty_weights_and_three_index_breadth(self) -> None:
        self.assertEqual(
            {
                key: S9_FILTER_WEIGHTS[key]
                for key in (
                    "delta",
                    "volume_breakout",
                    "order_book",
                    "supertrend",
                    "pcr",
                )
            },
            {
                "delta": 20,
                "volume_breakout": 12,
                "order_book": 12,
                "supertrend": 5,
                "pcr": 5,
            },
        )

        def state(open_price: float, close: float) -> SimpleNamespace:
            latest = SimpleNamespace(
                candle_time=datetime(2026, 9, 25, 12, 0),
                open=open_price,
                close=close,
            )
            return SimpleNamespace(timeframe="3m", latest=latest, previous=None)

        self.engine._record_s9_index_breadth_state("NIFTY 50", state(100, 101))
        self.engine._record_s9_index_breadth_state("SENSEX", state(200, 201))
        self.engine._record_s9_index_breadth_state("BANK NIFTY", state(300, 299))

        bullish = self.engine._s9_index_breadth_score("bullish")
        bearish = self.engine._s9_index_breadth_score("bearish")
        self.assertEqual(bullish["points"], 2)
        self.assertEqual(bearish["points"], 1)
        self.assertEqual(bullish["max_points"], 3)

    def test_bank_finnifty_vwap_and_volume_use_option_premium_state(self) -> None:
        row = contract()
        state = premium_state()
        evaluation = self.engine._evaluate_s9_contract(
            contract=row,
            option_chain=chain("FINNIFTY", row),
            underlying="FINNIFTY",
            option_type="CE",
            direction="bullish",
            market_filters={},
            strike_selection_mode="atm",
            previous_option_map={},
            apply_oi_change_filter=False,
            underlying_state=None,
            volume_breakout_state=None,
            stock_setup=False,
            volume_breakout_setup=True,
            target_index_setup=False,
            option_state=state,
            evaluation_time=datetime(2026, 9, 25, 9, 16, 30),
            index_breadth_score=3,
        )

        vwap = evaluation["contract_filters"]["vwap"]
        volume = evaluation["contract_filters"]["volume_breakout"]
        self.assertEqual(vwap["data"]["source"], "option_premium_3m_indicator_state")
        self.assertEqual(vwap["data"]["vwap"], 105.0)
        self.assertNotEqual(vwap["status"], "unavailable")
        self.assertEqual(volume["data"]["source"], "option_premium_3m_indicator_state")
        self.assertEqual(volume["data"]["volume"], 300.0)
        self.assertEqual(S9_INDEX_VOLUME_VALIDATION_START_SECONDS, 60.0)
        self.assertEqual(volume["data"]["volume_validation_start_seconds"], 60.0)
        self.assertTrue(volume["data"]["volume_validation_ready"])
        self.assertEqual(volume["weight"], 12)
        self.assertTrue(volume["passed"])
        self.assertEqual(evaluation["index_breadth_score"], 3)

    def test_individual_stock_volume_keeps_125_second_start(self) -> None:
        check = self.engine._check_stock_3m_volume_breakout(
            premium_state(option_symbol="RELIANCE 3000 CE"),
            "bullish",
            now_market=datetime(2026, 9, 25, 9, 16, 30),
        )

        self.assertEqual(S9_VOLUME_VALIDATION_START_SECONDS, 125.0)
        self.assertEqual(check["data"]["volume_validation_start_seconds"], 125.0)
        self.assertTrue(check["data"]["volume_pending"])
        self.assertTrue(check["passed"])

    def test_nifty_sensex_volume_is_contract_filter_with_five_points(self) -> None:
        row = contract(option_type="PE")
        state = premium_state(option_symbol="NIFTY 50 25000 PE")
        evaluation = self.engine._evaluate_s9_contract(
            contract=row,
            option_chain=chain("NIFTY 50", row),
            underlying="NIFTY 50",
            option_type="PE",
            direction="bearish",
            market_filters={},
            strike_selection_mode="atm",
            previous_option_map={},
            apply_oi_change_filter=False,
            target_index_setup=True,
            option_state=state,
            evaluation_time=datetime(2026, 9, 25, 9, 17, 5),
        )

        volume = evaluation["contract_filters"]["volume_breakout"]
        self.assertEqual(volume["data"]["source"], "option_premium_3m_indicator_state")
        self.assertEqual(volume["data"]["validation_window_start_seconds"], 60.0)
        self.assertTrue(volume["data"]["validation_ready"])
        self.assertEqual(volume["weight"], 5)
        self.assertTrue(volume["passed"])
        self.assertIn("PE premium", volume["reason"])


if __name__ == "__main__":
    unittest.main()
