import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.services.option_types import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.dhan_service import DhanService  # noqa: E402
from app.services.screener_engine import S9_TARGET_INDEX_FILTER_WEIGHTS, ScreenerEngine  # noqa: E402


def contract(
    option_type="CE",
    *,
    strike=25000,
    ltp=102.5,
    oi=1000,
    volume=100,
    delta=0.60,
    theta=-0.05,
    vega=0.1,
    gamma=0.045,
    bid_price=100.00,
    ask_price=100.04,
    bid_qty=56,
    ask_qty=44,
):
    return OptionContract(
        security_id=f"{strike}{option_type}",
        strike=strike,
        option_type=option_type,
        ltp=ltp,
        oi=oi,
        oi_change=1,
        volume=volume,
        delta=delta,
        theta=theta,
        vega=vega,
        gamma=gamma,
        bid_price=bid_price,
        ask_price=ask_price,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
    )


def chain(*contracts, underlying="NIFTY 50", now=None):
    return OptionChainSnapshot(
        underlying=underlying,
        spot_price=25000,
        atm_strike=25000,
        expiry_date=date.today() + timedelta(days=7),
        snapshot_time=now or datetime(2026, 9, 23, 10, 0),
        contracts=tuple(contracts),
    )


def completed_state(close, ema20):
    return SimpleNamespace(
        timeframe="15m",
        latest=SimpleNamespace(
            candle_time=datetime(2026, 9, 23, 9, 45),
            close=close,
            ema20=ema20,
            rsi=None,
        ),
        previous=None,
    )


def target_3m_state(now: datetime):
    previous = SimpleNamespace(
        candle_time=now - timedelta(minutes=3),
        open=99.0,
        high=100.0,
        low=98.0,
        close=99.0,
        volume=100.0,
        supertrend=100.0,
        supertrend_direction="down",
    )
    latest = SimpleNamespace(
        candle_time=now - timedelta(seconds=175),
        open=98.0,
        high=102.0,
        low=97.0,
        close=99.0,
        volume=150.0,
        volume_ma20=100.0,
        stoch_rsi_k=60.0,
        stoch_rsi_d=40.0,
        supertrend=98.0,
        supertrend_direction="up",
    )
    return SimpleNamespace(timeframe="3m", latest=latest, previous=previous)


def target_option_3m_state(now: datetime):
    latest = SimpleNamespace(
        candle_time=now - timedelta(seconds=175),
        open=100.0,
        high=103.0,
        low=99.0,
        close=102.5,
        volume=150.0,
        volume_ma=100.0,
        volume_ma20=100.0,
        vwap=100.0,
        rsi=55.0,
    )
    return SimpleNamespace(timeframe="3m", latest=latest, previous=None)


class TestNiftySensexS9Rules(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            database_url="sqlite:///./test.db",
            redis_url="memory://",
            underlying_symbol="NIFTY 50",
            nifty_index_symbol="NIFTY 50",
        )
        self.engine = ScreenerEngine(self.settings)

    def tearDown(self):
        self.engine._cache._GLOBAL_MEMORY_CACHE.clear()

    def test_target_filter_weights_total_100(self):
        self.assertEqual(
            S9_TARGET_INDEX_FILTER_WEIGHTS,
            {
                "sweep": 10,
                "macro_trend": 10,
                "vwap": 10,
                "order_book": 20,
                "pcr": 5,
                "pcr_shift": 5,
                "delta": 10,
                "theta": 5,
                "stoch_rsi": 5,
                "supertrend": 5,
                "volume_breakout": 5,
                "vega_vix": 5,
                "gamma": 5,
                "spread": 0,
            },
        )
        self.assertEqual(sum(S9_TARGET_INDEX_FILTER_WEIGHTS.values()), 100)

    def test_completed_15m_close_above_ema20_scans_ce_only(self):
        sides = []
        self.engine._eligible_s9_contracts = lambda _chain, side, _mode: sides.append(side) or []
        rows = self.engine._run_s9(
            states_by_timeframe={"s9_15m_completed": {"NIFTY 50": completed_state(101, 100)}},
            s1_signals=[],
            option_chain=chain(contract("CE"), contract("PE", delta=-0.60)),
            now_market=datetime(2026, 9, 23, 10, 0),
        )
        self.assertEqual(sides, ["CE"])
        self.assertEqual(rows[0].payload["effective_direction"], "bullish")

    def test_completed_15m_close_below_ema20_scans_pe_only(self):
        sides = []
        self.engine._eligible_s9_contracts = lambda _chain, side, _mode: sides.append(side) or []
        self.engine._run_s9(
            states_by_timeframe={"s9_15m_completed": {"NIFTY 50": completed_state(99, 100)}},
            s1_signals=[],
            option_chain=chain(contract("CE"), contract("PE", delta=-0.60)),
            now_market=datetime(2026, 9, 23, 10, 0),
        )
        self.assertEqual(sides, ["PE"])

    def test_sensex_uses_the_same_completed_15m_ema20_gate(self):
        engine = ScreenerEngine(Settings(database_url="sqlite:///./test.db", redis_url="memory://", underlying_symbol="SENSEX", nifty_index_symbol="SENSEX"))
        self.assertEqual(
            engine._resolve_s9_target_index_macro_direction(
                states_by_timeframe={"s9_15m_completed": {"SENSEX": completed_state(101, 100)}},
                underlying_symbol="SENSEX",
            ),
            ("bullish", "buy"),
        )

    def test_equal_missing_or_incomplete_15m_never_scans(self):
        cases = [
            {"s9_15m_completed": {"NIFTY 50": completed_state(100, 100)}},
            {"s9_15m_completed": {"NIFTY 50": completed_state(101, None)}},
            {"15m": {"NIFTY 50": completed_state(101, 100)}},
            {"1m": {"NIFTY 50": completed_state(101, 100)}},
        ]
        for states in cases:
            with self.subTest(states=states.keys()):
                sides = []
                self.engine._eligible_s9_contracts = lambda _chain, side, _mode: sides.append(side) or []
                rows = self.engine._run_s9(
                    states_by_timeframe=states,
                    s1_signals=[],
                    option_chain=chain(contract()),
                    now_market=datetime(2026, 9, 23, 10, 0),
                )
                self.assertEqual(sides, [])
                self.assertEqual(rows[0].signal, "neutral")

    def test_premium_vwap_floor_and_extension_boundaries(self):
        self.engine._cache.get_json = lambda _key: {"vwap": 100.0}
        self.assertTrue(self.engine._check_target_vwap_extension("NIFTY 50 25000 CE", contract(ltp=102.5))["passed"])
        self.assertFalse(self.engine._check_target_vwap_extension("NIFTY 50 25000 CE", contract(ltp=102.5001))["passed"])
        self.assertFalse(self.engine._check_target_vwap_extension("NIFTY 50 25000 CE", contract(ltp=100.0))["passed"])

    def test_order_book_corridor_and_five_consecutive_seconds(self):
        ce = contract(bid_qty=55, ask_qty=45)
        start = datetime(2026, 9, 23, 10, 0, 0)
        for offset in range(5):
            check = self.engine._check_target_order_book(ce, "bullish", option_symbol="NIFTY 50 25000 CE", now_market=start + timedelta(seconds=offset))
            self.assertFalse(check["passed"])
        self.assertTrue(self.engine._check_target_order_book(ce, "bullish", option_symbol="NIFTY 50 25000 CE", now_market=start + timedelta(seconds=5))["passed"])
        self.assertFalse(self.engine._check_target_order_book(contract(bid_qty=59, ask_qty=41), "bullish", option_symbol="NIFTY 50 25000 CE", now_market=start + timedelta(seconds=6))["passed"])

        pe = contract("PE", delta=-0.60, bid_qty=42, ask_qty=58)
        for offset in range(6):
            pe_check = self.engine._check_target_order_book(pe, "bearish", option_symbol="NIFTY 50 25000 PE", now_market=start + timedelta(seconds=offset))
        self.assertTrue(pe_check["passed"])
        self.assertFalse(self.engine._check_target_order_book(contract("PE", delta=-0.60, bid_qty=41, ask_qty=59), "bearish", option_symbol="NIFTY 50 25000 PE", now_market=start + timedelta(seconds=6))["passed"])

    def test_target_pcr_ranges_and_weight(self):
        ce_pass = chain(contract("CE", oi=1000), contract("PE", delta=-0.60, oi=750))
        ce_fail = chain(contract("CE", oi=1000), contract("PE", delta=-0.60, oi=749))
        pe_pass = chain(contract("CE", oi=1000), contract("PE", delta=-0.60, oi=600))
        pe_fail = chain(contract("CE", oi=1000), contract("PE", delta=-0.60, oi=599))

        self.assertTrue(self.engine._check_target_pcr(ce_pass, "bullish")["passed"])
        self.assertFalse(self.engine._check_target_pcr(ce_fail, "bullish")["passed"])
        self.assertTrue(self.engine._check_target_pcr(pe_pass, "bearish")["passed"])
        self.assertFalse(self.engine._check_target_pcr(pe_fail, "bearish")["passed"])

        status = self.engine._s9_filter_status(
            "pcr",
            self.engine._check_target_pcr(ce_pass, "bullish"),
            "bullish",
        )
        self.assertEqual(status["weight"], 5)
        self.assertEqual(status["name"], "PCR")

    def test_spread_strict_boundary(self):
        self.assertTrue(self.engine._check_target_spread(contract(bid_price=100, ask_price=100.0499))["passed"])
        self.assertFalse(self.engine._check_target_spread(contract(bid_price=100, ask_price=100.05))["passed"])

    def test_delta_boundaries(self):
        for value in (0.55, 0.65):
            self.assertTrue(self.engine._check_target_delta_option("NIFTY 50 25000 CE", "CE", contract(delta=value))["passed"])
        for value in (-0.65, -0.55):
            self.assertTrue(self.engine._check_target_delta_option("NIFTY 50 25000 PE", "PE", contract("PE", delta=value))["passed"])
        self.assertFalse(self.engine._check_target_delta_option("NIFTY 50 25000 CE", "CE", contract(delta=0.5499))["passed"])
        self.assertFalse(self.engine._check_target_delta_option("NIFTY 50 25000 PE", "PE", contract("PE", delta=-0.5499))["passed"])

    def test_normalized_theta_cap(self):
        passing = self.engine._check_target_theta(contract(ltp=100, theta=-0.05))
        failing = self.engine._check_target_theta(contract(ltp=100, theta=-0.0501))
        self.assertTrue(passing["passed"])
        self.assertEqual(passing["data"]["provider_unit"], "absolute INR option-premium points per day")
        self.assertFalse(failing["passed"])

    def test_vega_and_india_vix_both_required(self):
        self.assertTrue(self.engine._check_target_vega_vix(contract(vega=0.01), {"india_vix": {"value": 17.99}})["passed"])
        self.assertFalse(self.engine._check_target_vega_vix(contract(vega=0), {"india_vix": {"value": 17.99}})["passed"])
        self.assertFalse(self.engine._check_target_vega_vix(contract(vega=0.01), {"india_vix": {"value": 18.0}})["passed"])
        self.assertFalse(self.engine._check_target_vega_vix(contract(vega=0.01), {})["passed"])

    def test_gamma_boundaries(self):
        self.assertTrue(self.engine._check_target_gamma(contract(gamma=0.035))["passed"])
        self.assertTrue(self.engine._check_target_gamma(contract(gamma=0.055))["passed"])
        self.assertFalse(self.engine._check_target_gamma(contract(gamma=0.0349))["passed"])
        self.assertFalse(self.engine._check_target_gamma(contract(gamma=0.0551))["passed"])

    def test_target_index_selection_scans_delta_eligible_window_not_atm_only(self):
        rows = tuple(contract(strike=strike) for strike in range(24800, 25201, 50))
        selected = self.engine._eligible_s9_contracts(chain(*rows), "CE", "delta_window")
        self.assertGreater(len(selected), 1)
        self.assertIn(25000, [row.strike for row in selected])

    def test_all_target_filters_confirm_buy_call_without_one_minute_state(self):
        now = datetime(2026, 9, 23, 10, 2, 55)
        ce = contract("CE")
        pe = contract("PE", delta=-0.60, volume=104, bid_qty=44, ask_qty=56)
        self.engine._cache.set_json("vwap:NIFTY 50 25000 CE:5m", {"vwap": 100.0})
        self.engine._cache.set_json(
            self.engine._s9_volume_pcr_history_key("NIFTY 50"),
            [{"time": (now - timedelta(minutes=3)).isoformat(), "volume_pcr": 1.0}],
        )
        for offset in range(5):
            self.engine._check_target_order_book(
                ce,
                "bullish",
                option_symbol="NIFTY 50 25000 CE",
                now_market=now - timedelta(seconds=5 - offset),
            )
        rows = self.engine._run_s9(
            states_by_timeframe={
                "3m": {"NIFTY 50": target_3m_state(now)},
                "s9_15m_completed": {"NIFTY 50": completed_state(101, 100)},
            },
            s1_signals=[],
            option_chain=chain(ce, pe, now=now),
            now_market=now,
            option_states={(25000.0, "CE"): target_option_3m_state(now)},
            macro_context={"india_vix": {"value": 17.5}},
        )
        self.assertEqual(rows[0].signal, "BUY_CALL")
        self.assertTrue(rows[0].payload["execution_allowed"])
        self.assertEqual(
            set(rows[0].payload["filters"]),
            {"sweep", "pcr", "delta", "gamma", "vwap", "order_book", "stoch_rsi", "supertrend", "volume_breakout"},
        )
        self.assertEqual(rows[0].payload["score"], 100)
        self.assertEqual(rows[0].payload["filters"]["order_book"]["weight"], 20)
        self.assertEqual(rows[0].payload["filters"]["pcr"]["weight"], 5)
        for hidden in ("macro_trend", "theta", "vega_vix", "spread", "pcr_shift"):
            self.assertNotIn(hidden, rows[0].payload["filters"])
            self.assertNotIn(hidden, rows[0].payload["market_filters"])
            self.assertNotIn(hidden, rows[0].payload["contract_filters"])
            self.assertNotIn(hidden, rows[0].payload["selected_contract_evaluation"]["filters"])
            self.assertNotIn(hidden, rows[0].payload["scanned_strikes"][0]["filters"])

    def test_missing_required_live_fields_fail_closed(self):
        missing = contract(theta=None, vega=None, gamma=None, bid_price=None, ask_price=None, bid_qty=None, ask_qty=None)
        self.engine._cache.get_json = lambda _key: None
        checks = (
            self.engine._check_target_theta(missing),
            self.engine._check_target_vega_vix(missing, {}),
            self.engine._check_target_gamma(missing),
            self.engine._check_target_spread(missing),
            self.engine._check_target_vwap_extension("NIFTY 50 25000 CE", missing),
            self.engine._check_target_order_book(missing, "bullish", option_symbol="NIFTY 50 25000 CE", now_market=datetime(2026, 9, 23, 10, 0)),
        )
        self.assertTrue(all(not check["passed"] for check in checks))
        self.assertTrue(all("unavailable" in check["reason"].lower() for check in checks))

    def test_non_target_selection_and_delta_rules_are_unchanged(self):
        cases = (("BANK NIFTY", 56000), ("FINNIFTY", 23500), ("RELIANCE", 2900))
        for symbol, atm in cases:
            with self.subTest(symbol=symbol):
                engine = ScreenerEngine(Settings(database_url="sqlite:///./test.db", redis_url="memory://", underlying_symbol=symbol, nifty_index_symbol=symbol, nifty_symbols=("RELIANCE",)))
                strikes = (atm - 200, atm - 100, atm, atm + 100, atm + 200, atm + 300)
                snapshot = chain(*(contract(strike=value) for value in strikes), underlying=symbol)
                snapshot = OptionChainSnapshot(**{**snapshot.__dict__, "atm_strike": atm, "spot_price": atm})
                self.assertEqual([row.strike for row in engine._eligible_s9_contracts(snapshot, "CE")], [atm, atm - 100, atm + 100, atm - 200, atm + 200])
                self.assertEqual(engine._s9_strike_selection_mode(snapshot, datetime(2026, 9, 23, 10, 0), symbol), "atm")
        self.assertTrue(engine._check_delta_option("RELIANCE 2900 CE", "CE", contract(strike=2900, delta=0.65))["passed"])

    def test_groww_parser_maps_real_depth_and_all_required_greeks(self):
        payload = {
            "trading_symbol": "NIFTY26SEP25000CE",
            "ltp": 100,
            "open_interest": 1000,
            "oi_day_change": 10,
            "volume": 500,
            "greeks": {"delta": 0.6, "theta": -0.05, "vega": 0.2, "gamma": 0.045},
            "depth": {"buy": [{"price": 99.98, "quantity": 56}], "sell": [{"price": 100.02, "quantity": 44}]},
        }
        parsed = DhanService.__new__(DhanService)._parse_groww_contract(payload, strike=25000, option_type="CE")
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.delta, parsed.theta, parsed.vega, parsed.gamma), (0.6, -0.05, 0.2, 0.045))
        self.assertEqual((parsed.bid_price, parsed.ask_price, parsed.bid_qty, parsed.ask_qty), (99.98, 100.02, 56.0, 44.0))


if __name__ == "__main__":
    unittest.main()
