import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.config import DhanSettings  # noqa: E402
from app.services.dhan_client import OptionChainSnapshot, OptionContract  # noqa: E402
from app.services.indicator_engine import SymbolIndicatorState  # noqa: E402
from app.services.screener_engine import ScreenerEngine, ScreenerSignal  # noqa: E402
from app.utils.indicators import IndicatorPoint  # noqa: E402


_EMA9_FROM_MCGINLEY = object()


def _point(*, close, mcginley, rsi, macd, macd_signal, ema9=_EMA9_FROM_MCGINLEY):
    return IndicatorPoint(
        candle_time=datetime(2026, 5, 1, 9, 20, 0),
        open=float(close),
        high=float(close),
        low=float(close),
        close=float(close),
        volume=1.0,
        volume_ma=None,
        rsi=None if rsi is None else float(rsi),
        macd=None if macd is None else float(macd),
        macd_signal=None if macd_signal is None else float(macd_signal),
        macd_histogram=None,
        ema9=(
            None
            if ema9 is None
            else float(mcginley if ema9 is _EMA9_FROM_MCGINLEY else ema9)
        ),
        mcginley=None if mcginley is None else float(mcginley),
        sma=None,
        bias="neutral",
        strength=0.5,
    )


def _state(timeframe: str, prev: IndicatorPoint | None, curr: IndicatorPoint) -> SymbolIndicatorState:
    return SymbolIndicatorState(symbol="BANK NIFTY", timeframe=timeframe, latest=curr, previous=prev)


def _option_chain(option_type: str = "CE", strike: float = 56000.0) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="BANK NIFTY",
        spot_price=56000.0,
        atm_strike=int(strike),
        expiry_date=date.today() + timedelta(days=7),
        snapshot_time=datetime.utcnow(),
        contracts=(
            OptionContract(
                security_id="12345",
                strike=strike,
                option_type=option_type,
                ltp=100.0,
                oi=1000.0,
                oi_change=10.0,
                volume=500.0,
            ),
        ),
    )


def _option_chain_many(contracts: tuple[OptionContract, ...], atm: int = 56000) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="BANK NIFTY",
        spot_price=float(atm),
        atm_strike=atm,
        expiry_date=date.today() + timedelta(days=7),
        snapshot_time=datetime.utcnow(),
        contracts=contracts,
    )


def _contract(
    strike: float,
    option_type: str,
    *,
    oi: float = 1000.0,
    delta=None,
    theta=-15.0,
    bid_qty: float = 700.0,
    ask_qty: float = 300.0,
) -> OptionContract:
    return OptionContract(
        security_id=f"{int(strike)}{option_type}",
        strike=strike,
        option_type=option_type,
        ltp=100.0,
        oi=oi,
        oi_change=10.0,
        volume=500.0,
        delta=delta,
        theta=theta,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
    )


class TestS9Screener(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            nifty_symbols=("HDFCBANK", "ICICIBANK", "SBIN"),
            underlying_symbol="BANK NIFTY",
            nifty_index_symbol="BANK NIFTY",
        )
        self.engine = ScreenerEngine(settings)

    def test_dhan_option_instrument_sanitizes_env_typo(self) -> None:
        settings = DhanSettings(options_instrument="OPTIDX+")
        self.assertEqual(settings.options_instrument, "OPTIDX")

    def test_stock_bank_finnifty_s9_scan_includes_atm_plus_two_each_side(self) -> None:
        contracts = tuple(_contract(strike, "CE") for strike in (55800, 55900, 56000, 56100, 56200, 56300))
        chain = _option_chain_many(contracts, atm=56000)

        selected = self.engine._eligible_s9_contracts(chain, "CE")

        self.assertEqual([contract.strike for contract in selected], [56000, 55900, 56100, 55800, 56200])

    def test_option_chain_validator_accepts_valid_snapshot(self) -> None:
        validation = self.engine.validate_option_chain(_option_chain())

        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.reason, "ok")
        self.assertEqual(validation.valid_contract_count, 1)

    def test_option_chain_validator_rejects_none(self) -> None:
        validation = self.engine.validate_option_chain(None)

        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.reason, "option_chain_missing")

    def test_option_chain_validator_rejects_empty_dict_and_list(self) -> None:
        self.assertEqual(self.engine.validate_option_chain({}).reason, "option_chain_empty")
        self.assertEqual(self.engine.validate_option_chain([]).reason, "option_chain_empty")

    def test_option_chain_validator_rejects_missing_chain_key(self) -> None:
        validation = self.engine.validate_option_chain({"data": {"unexpected": []}})

        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.reason, "malformed_provider_response")

    def test_option_chain_validator_skips_one_malformed_strike(self) -> None:
        validation = self.engine.validate_option_chain(
            {
                "oc": {
                    "bad": {"ce": {"security_id": "bad"}},
                    "56000": {"ce": {"security_id": "123"}},
                }
            }
        )

        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.reason, "ok")
        self.assertEqual(validation.valid_strike_count, 1)
        self.assertEqual(validation.malformed_row_count, 1)

    def test_option_chain_validator_accepts_mixed_valid_and_malformed_strikes(self) -> None:
        validation = self.engine.validate_option_chain(
            {
                "data": {
                    "oc": {
                        "56000": {"ce": {"security_id": "123"}},
                        "56050": "not-a-row",
                        "56100": {"pe": {"security_id": "456"}},
                    }
                }
            }
        )

        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.valid_contract_count, 2)
        self.assertEqual(validation.valid_strike_count, 2)
        self.assertEqual(validation.malformed_row_count, 1)

    def test_option_chain_validator_accepts_only_ce_or_only_pe(self) -> None:
        ce_validation = self.engine.validate_option_chain({"oc": {"56000": {"ce": {"security_id": "123"}}}})
        pe_validation = self.engine.validate_option_chain({"oc": {"56000": {"pe": {"security_id": "456"}}}})

        self.assertTrue(ce_validation.is_valid)
        self.assertTrue(pe_validation.is_valid)

    def test_option_chain_validator_rejects_no_valid_contracts(self) -> None:
        validation = self.engine.validate_option_chain({"oc": {"56000": {"xx": {"security_id": "123"}}}})

        self.assertFalse(validation.is_valid)
        self.assertEqual(validation.reason, "no_valid_contracts")

    def test_run_s9_with_valid_option_chain_no_missing_validator_attribute(self) -> None:
        self._allow_all_filters(self.engine)

        rows = self.engine._run_s9(
            states_by_timeframe=self._weak_bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="CE", strike=56000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bullish"},
        )

        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0].reason, "OPTION_CHAIN_UNAVAILABLE")

    def test_s9_ignores_s1_and_override_and_uses_state_only(self) -> None:
        self._allow_all_filters(self.engine)

        rows = self.engine._run_s9(
            states_by_timeframe=self._weak_bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="CE", strike=56000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bearish"},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["underlying_direction"], "neutral")
        self.assertEqual(rows[0].payload["underlying_signal"], "neutral")
        self.assertEqual(rows[0].payload["direction_source"], "s9_state")

    def _bullish_index_states(self) -> dict[str, dict[str, SymbolIndicatorState]]:
        prev = _point(close=99, mcginley=100, rsi=54, macd=0, macd_signal=0)
        curr = _point(close=101, mcginley=100, rsi=56, macd=2, macd_signal=1)
        return {
            "3m": {"BANK NIFTY": _state("3m", prev, curr)},
            "15m": {"BANK NIFTY": _state("15m", prev, curr)},
            "30m": {"BANK NIFTY": _state("30m", prev, curr)},
        }

    def _neutral_index_states(self) -> dict[str, dict[str, SymbolIndicatorState]]:
        prev = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        curr = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1, ema9=102)
        return {
            "3m": {"BANK NIFTY": _state("3m", prev, curr)},
            "15m": {"BANK NIFTY": _state("15m", prev, curr)},
            "30m": {"BANK NIFTY": _state("30m", prev, curr)},
        }

    def _bearish_index_states(self) -> dict[str, dict[str, SymbolIndicatorState]]:
        prev = _point(close=102, mcginley=100, rsi=46, macd=0, macd_signal=1)
        curr = _point(close=98, mcginley=100, rsi=44, macd=-2, macd_signal=0)
        return {
            "3m": {"BANK NIFTY": _state("3m", prev, curr)},
            "15m": {"BANK NIFTY": _state("15m", prev, curr)},
            "30m": {"BANK NIFTY": _state("30m", prev, curr)},
        }

    def _weak_bullish_index_states(self) -> dict[str, dict[str, SymbolIndicatorState]]:
        bullish_prev = _point(close=99, mcginley=100, rsi=54, macd=0, macd_signal=0)
        bullish_curr = _point(close=101, mcginley=100, rsi=56, macd=2, macd_signal=1)
        neutral_prev = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        neutral_curr = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1, ema9=102)
        return {
            "3m": {"BANK NIFTY": _state("3m", bullish_prev, bullish_curr)},
            "15m": {"BANK NIFTY": _state("15m", neutral_prev, neutral_curr)},
            "30m": {"BANK NIFTY": _state("30m", neutral_prev, neutral_curr)},
        }

    def _weak_bearish_index_states(self) -> dict[str, dict[str, SymbolIndicatorState]]:
        bearish_prev = _point(close=102, mcginley=100, rsi=46, macd=0, macd_signal=1)
        bearish_curr = _point(close=98, mcginley=100, rsi=44, macd=-2, macd_signal=0)
        neutral_prev = _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1)
        neutral_curr = _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1, ema9=102)
        return {
            "3m": {"BANK NIFTY": _state("3m", bearish_prev, bearish_curr)},
            "15m": {"BANK NIFTY": _state("15m", neutral_prev, neutral_curr)},
            "30m": {"BANK NIFTY": _state("30m", neutral_prev, neutral_curr)},
        }

    def _mixed_index_states(self, *, thirty: str, fifteen: str) -> dict[str, dict[str, SymbolIndicatorState]]:
        points = {
            "bullish": (
                _point(close=99, mcginley=100, rsi=54, macd=0, macd_signal=0),
                _point(close=101, mcginley=100, rsi=56, macd=2, macd_signal=1),
            ),
            "bearish": (
                _point(close=102, mcginley=100, rsi=46, macd=0, macd_signal=1),
                _point(close=98, mcginley=100, rsi=44, macd=-2, macd_signal=0),
            ),
            "neutral": (
                _point(close=101, mcginley=100, rsi=55, macd=2, macd_signal=1),
                _point(close=102, mcginley=100, rsi=55, macd=2, macd_signal=1, ema9=102),
            ),
        }
        three_prev, three_curr = points["bullish"]
        thirty_prev, thirty_curr = points[thirty]
        fifteen_prev, fifteen_curr = points[fifteen]
        return {
            "3m": {"BANK NIFTY": _state("3m", three_prev, three_curr)},
            "15m": {"BANK NIFTY": _state("15m", fifteen_prev, fifteen_curr)},
            "30m": {"BANK NIFTY": _state("30m", thirty_prev, thirty_curr)},
        }

    def _conflicting_s1_signals(self) -> list[ScreenerSignal]:
        return [
            ScreenerSignal(screener="S1", symbol="HDFCBANK", signal="strong_sell", confidence=0.95, reason="", payload={}),
            ScreenerSignal(screener="S1", symbol="ICICIBANK", signal="sell", confidence=0.9, reason="", payload={}),
            ScreenerSignal(screener="S1", symbol="SBIN", signal="strong_sell", confidence=0.85, reason="", payload={}),
        ]

    def _allow_all_filters(self, engine: ScreenerEngine) -> None:
        engine._check_sweep_buy = lambda *_: {"passed": True, "reason": "Sweep passed"}
        engine._check_sweep_sell = lambda *_: {"passed": True, "reason": "Sweep passed"}
        engine._check_delta_option = lambda *_: {"passed": True, "reason": ""}
        engine._check_theta_option = lambda *_: {"passed": True, "reason": ""}
        engine._check_pcr_buy = lambda *_: {"passed": True, "reason": ""}
        engine._check_pcr_sell = lambda *_: {"passed": True, "reason": ""}
        engine._check_pcr_shift_buy = lambda *_: {"passed": True, "reason": ""}
        engine._check_pcr_shift_sell = lambda *_: {"passed": True, "reason": ""}
        engine._check_vwap_option = lambda *_: {"passed": True, "reason": ""}
        engine._check_order_book_buy = lambda *_: {"passed": True, "reason": ""}
        engine._check_order_book_sell = lambda *_: {"passed": True, "reason": ""}

    def _run_success(self, states, option_type: str, s1_signals: list[ScreenerSignal] | None = None):
        engine = self.engine
        self._allow_all_filters(engine)
        return engine._run_s9(
            states_by_timeframe=states,
            s1_signals=s1_signals or [],
            option_chain=_option_chain(option_type=option_type, strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

    def test_s9_neutral_returns_single_neutral_row(self) -> None:
        rows = self.engine._run_s9(
            states_by_timeframe=self._neutral_index_states(),
            s1_signals=self._conflicting_s1_signals(),
            option_chain=_option_chain(),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.symbol, "BANK NIFTY")
        self.assertEqual(row.signal, "neutral")
        self.assertEqual(row.payload["rejection_reason"], "BANK_NIFTY_NEUTRAL")
        self.assertEqual(row.payload["bank_nifty_direction"], "neutral")
        self.assertEqual(row.payload["bank_nifty_signal"], "neutral")
        self.assertEqual(row.payload["signal_time"], "2026-05-01T09:30:00")
        self.assertTrue(row.payload["signal_only"])
        self.assertFalse(row.payload["execution_allowed"])

    def test_s9_buy_call_from_bank_nifty_buy(self) -> None:
        rows = self._run_success(self._weak_bullish_index_states(), "CE")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.signal, "neutral")
        self.assertEqual(row.payload["bank_nifty_direction"], "neutral")
        self.assertEqual(row.payload["bank_nifty_signal"], "neutral")
        self.assertEqual(row.payload["option_type"], None)
        self.assertFalse(row.payload["confirmed"])

    def test_s9_strong_buy_call_from_bank_nifty_strong_buy(self) -> None:
        rows = self._run_success(
            self._bullish_index_states(),
            "CE",
            s1_signals=self._conflicting_s1_signals(),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.signal, "BUY_CALL")
        self.assertEqual(row.payload["bank_nifty_direction"], "bullish")
        self.assertEqual(row.payload["bank_nifty_signal"], "strong_buy")
        self.assertEqual(row.payload["option_type"], "CE")
        self.assertEqual(row.payload["strike"], 58000)
        self.assertEqual(row.payload["option_symbol"], "BANK NIFTY 58000 CE")
        self.assertEqual(row.payload["signal"], "buy_call")
        self.assertEqual(row.payload["signal_time"], "2026-05-01T09:30:00")
        self.assertTrue(row.payload["confirmed"])
        self.assertEqual(
            set(row.payload["filters"]),
            {"sweep", "delta", "theta", "pcr", "pcr_shift", "vwap", "order_book"},
        )
        self.assertTrue(all(item["passed"] for item in row.payload["filters"].values()))

    def test_s9_bearish_buy_put_from_bank_nifty_sell(self) -> None:
        rows = self._run_success(self._weak_bearish_index_states(), "PE")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.signal, "neutral")
        self.assertEqual(row.payload["bank_nifty_direction"], "neutral")
        self.assertEqual(row.payload["bank_nifty_signal"], "neutral")
        self.assertEqual(row.payload["option_type"], None)

    def test_s9_bearish_strong_buy_put_from_bank_nifty_strong_sell(self) -> None:
        rows = self._run_success(self._bearish_index_states(), "PE")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.signal, "BUY_PUT")
        self.assertEqual(row.payload["bank_nifty_direction"], "bearish")
        self.assertEqual(row.payload["bank_nifty_signal"], "strong_sell")
        self.assertEqual(row.payload["option_type"], "PE")
        self.assertEqual(row.payload["signal"], "buy_put")

    def test_s9_direction_uses_ema9_and_ignores_rsi_macd(self) -> None:
        prev = _point(close=100, mcginley=100, rsi=50, macd=0, macd_signal=0)
        bullish_curr = _point(close=101, mcginley=100, rsi=10, macd=-2, macd_signal=1, ema9=100)
        bearish_curr = _point(close=99, mcginley=100, rsi=90, macd=2, macd_signal=-1, ema9=100)
        neutral_curr = _point(close=100, mcginley=100, rsi=90, macd=2, macd_signal=-1, ema9=100)

        self.assertEqual(self.engine._s9_ema9_direction(_state("60m", prev, bullish_curr)), "bullish")
        self.assertEqual(self.engine._s9_ema9_direction(_state("60m", prev, bearish_curr)), "bearish")
        self.assertEqual(self.engine._s9_ema9_direction(_state("60m", prev, neutral_curr)), "neutral")

    def test_s9_option_chain_unavailable_returns_single_row(self) -> None:
        rows = self.engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=None,
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.payload["rejection_reason"], "OPTION_CHAIN_UNAVAILABLE")
        self.assertEqual(row.payload["bank_nifty_direction"], "bullish")
        self.assertEqual(row.payload["bank_nifty_signal"], "strong_buy")
        self.assertEqual(row.payload["option_type"], "CE")
        self.assertTrue(row.payload["signal_only"])
        self.assertFalse(row.payload["execution_allowed"])

    def test_s9_works_with_empty_s1_signals(self) -> None:
        rows = self._run_success(self._bullish_index_states(), "CE", s1_signals=[])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].signal, "BUY_CALL")

    def test_s9_manual_bullish_evaluates_ce_and_blocks_execution(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        rows = engine._run_s9(
            states_by_timeframe=self._neutral_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="CE", strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bullish", "expires_at": "2026-05-01T10:00:00"},
        )
        row = rows[0]
        self.assertEqual(row.signal, "neutral")
        self.assertEqual(row.payload["direction_source"], "s9_state")
        self.assertEqual(row.payload["effective_direction"], "neutral")
        self.assertEqual(row.payload["option_type"], None)
        self.assertFalse(row.payload["is_manual_override"])
        self.assertFalse(row.payload["execution_allowed"])

    def test_s9_manual_bullish_selects_ce_contract_even_when_sweep_fails(self) -> None:
        rows = self.engine._run_s9(
            states_by_timeframe=self._neutral_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="CE", strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bullish"},
        )
        row = rows[0]
        self.assertEqual(row.payload["effective_direction"], "neutral")
        self.assertEqual(row.payload["option_type"], None)
        self.assertIsNone(row.payload["strike"])
        self.assertIsNone(row.payload["option_symbol"])
        self.assertEqual(row.payload["rejection_reason"], "BANK_NIFTY_NEUTRAL")

    def test_s9_manual_bearish_evaluates_pe_and_blocks_execution(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        rows = engine._run_s9(
            states_by_timeframe=self._neutral_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="PE", strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bearish"},
        )
        row = rows[0]
        self.assertEqual(row.signal, "neutral")
        self.assertEqual(row.payload["direction_source"], "s9_state")
        self.assertEqual(row.payload["effective_direction"], "neutral")
        self.assertEqual(row.payload["option_type"], None)
        self.assertFalse(row.payload["execution_allowed"])

    def test_s9_dynamic_scan_payload_exposes_every_scanned_strike(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda symbol, *_: {
            "passed": "56100" in symbol,
            "reason": "ok" if "56100" in symbol else "forced delta",
            "data": {"delta": 0.75 if "56100" in symbol else 0.62},
        }
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bullish"},
        )

        payload = rows[0].payload
        self.assertEqual(payload["strike_scan"]["total_scanned"], 2)
        self.assertEqual(payload["strike_scan"]["atm_strike"], 56000)
        self.assertEqual(payload["strike_scan"]["lower_range"], 56000)
        self.assertEqual(payload["strike_scan"]["upper_range"], 56100)
        self.assertEqual(payload["strike_scan"]["strike_step"], 100)
        self.assertEqual(payload["strike_scan"]["best_strike"], 56100)
        self.assertEqual(payload["strike_scan"]["selected_strike"], 56100)
        self.assertIsNone(payload["strike_scan"]["temporary_strike"])
        self.assertEqual([row["strike"] for row in payload["scanned_strikes"]], [56000, 56100])
        atm_row = next(row for row in payload["scanned_strikes"] if row["strike"] == 56000)
        selected_row = next(row for row in payload["scanned_strikes"] if row["strike"] == 56100)
        self.assertTrue(atm_row["is_atm"])
        self.assertFalse(atm_row["is_selected"])
        self.assertEqual(atm_row["first_failed_filter"], "delta")
        self.assertTrue(selected_row["is_best"])
        self.assertTrue(selected_row["is_selected"])
        self.assertEqual(len(selected_row["filters"]), 7)

    def test_s9_dynamic_scan_payload_marks_temporary_best_without_final_selection(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda symbol, *_: {"passed": False, "reason": "forced delta", "data": {"delta": 0.61}}
        engine._check_theta_option = lambda symbol, *_: {
            "passed": "56100" in symbol,
            "reason": "ok" if "56100" in symbol else "forced theta",
            "data": {"theta": -15.0},
        }
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
            override={"mode": "manual", "manual_direction": "bullish"},
        )

        payload = rows[0].payload
        self.assertIsNone(payload["selected_strike"])
        self.assertEqual(payload["strike_scan"]["best_strike"], 56100)
        self.assertEqual(payload["strike_scan"]["temporary_strike"], 56100)
        best_row = next(row for row in payload["scanned_strikes"] if row["strike"] == 56100)
        self.assertTrue(best_row["is_best"])
        self.assertFalse(best_row["is_selected"])
        self.assertEqual(best_row["passed_filter_count"], 6)
        self.assertEqual(best_row["first_failed_filter"], "delta")
        self.assertEqual(best_row["rejection_reason"], "forced delta")

    def test_s9_does_not_fetch_company_option_chain(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._dhan.get_option_chain = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("company option chain fetch should not occur")
        )

        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=self._conflicting_s1_signals(),
            option_chain=_option_chain(option_type="CE", strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].signal, "STRONG_BUY_CALL")

    def test_s9_no_valid_option_contract_rejection(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain(option_type="PE", strike=58000.0),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(rows[0].payload["rejection_reason"], "NO_VALID_OPTION_CONTRACT")
        self.assertTrue(rows[0].payload["signal_only"])
        self.assertFalse(rows[0].payload["execution_allowed"])

    def test_order_book_threshold_caps_at_58_percent(self) -> None:
        contract = _contract(56000, "CE", bid_qty=580.0, ask_qty=420.0)
        self.assertTrue(self.engine._check_order_book_buy(_option_chain("CE", 56000.0), contract)["passed"])
        self.assertFalse(self.engine._check_order_book_buy(_option_chain("CE", 56000.0), _contract(56000, "CE", bid_qty=590.0, ask_qty=410.0))["passed"])

        put_contract = _contract(56000, "PE", bid_qty=420.0, ask_qty=580.0)
        self.assertTrue(self.engine._check_order_book_sell(_option_chain("PE", 56000.0), put_contract)["passed"])
        self.assertFalse(self.engine._check_order_book_sell(_option_chain("PE", 56000.0), _contract(56000, "PE", bid_qty=410.0, ask_qty=590.0))["passed"])

    def test_s9_filter_rejection_paths(self) -> None:
        cases = [
            ("SWEEP_FAILED", "_check_sweep_buy"),
            ("DELTA_FAILED", "_check_delta_option"),
            ("THETA_FAILED", "_check_theta_option"),
            ("PCR_FAILED", "_check_pcr_buy"),
            ("PCR_SHIFT_FAILED", "_check_pcr_shift_buy"),
            ("VWAP_FAILED", "_check_vwap_option"),
            ("ORDER_BOOK_FAILED", "_check_order_book_buy"),
        ]
        for rejection_reason, failing_check in cases:
            with self.subTest(rejection_reason=rejection_reason):
                engine = ScreenerEngine(self.engine._settings)
                self._allow_all_filters(engine)
                setattr(engine, failing_check, lambda *_: {"passed": False, "reason": "forced failure"})
                rows = engine._run_s9(
                    states_by_timeframe=self._bullish_index_states(),
                    s1_signals=[],
                    option_chain=_option_chain(option_type="CE", strike=58000.0),
                    now_market=datetime(2026, 5, 1, 9, 30, 0),
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].payload["rejection_reason"], "NO_STRIKE_PASSED_ALL_FILTERS")
                self.assertIsNone(rows[0].payload["selected_strike"])
                self.assertEqual(
                    rows[0].payload["scanned_contract_evaluations"][0]["failed_filters"][0],
                    rejection_reason.removesuffix("_FAILED").lower(),
                )
                for key in (
                    "auto_direction",
                    "auto_signal",
                    "effective_direction",
                    "effective_signal",
                    "direction_source",
                    "is_manual_override",
                    "execution_allowed",
                    "passed_count",
                    "total_filters",
                ):
                    self.assertIn(key, rows[0].payload)

    def test_s9_rejection_exposes_evaluated_strike(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda symbol, *_: {"passed": False, "reason": "forced failure", "data": {"delta": 0.65}}
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload["rejection_reason"], "NO_STRIKE_PASSED_ALL_FILTERS")
        self.assertIsNone(rows[0].payload["selected_strike"])
        self.assertEqual(rows[0].payload["evaluated_strike"], 56000)
        self.assertEqual(rows[0].payload["evaluated_option_type"], "CE")
        self.assertEqual(rows[0].payload["evaluated_option_symbol"], "BANK NIFTY 56000 CE")
        self.assertIsInstance(rows[0].payload["scanned_contract_evaluations"], list)
        self.assertGreater(len(rows[0].payload["scanned_contract_evaluations"]), 0)

    def test_s9_delta_cache_key_uses_canonical_symbol(self) -> None:
        seen_keys: list[str] = []

        def fake_get_json(key: str):
            seen_keys.append(key)
            return {"delta": -0.75, "source": "unit-test"}

        self.engine._cache.get_json = fake_get_json
        check = self.engine._check_delta_option("BANKNIFTY ATM 57100 PE", "PE")
        self.assertTrue(check["passed"])
        self.assertEqual(seen_keys, ["option:delta:BANK NIFTY 57100 PE:5m"])
        self.assertEqual(check["data"]["lookup_key"], "option:delta:BANK NIFTY 57100 PE:5m")

    def test_s9_option_symbol_normalization(self) -> None:
        self.assertEqual(
            self.engine._canonical_option_symbol("BANKNIFTY 57100 PE"),
            "BANK NIFTY 57100 PE",
        )
        self.assertEqual(
            self.engine._canonical_option_symbol("BANK NIFTY ATM 57100 PE"),
            "BANK NIFTY 57100 PE",
        )
        self.assertEqual(
            self.engine._build_option_symbol("BANK NIFTY", 57100.0, "pe"),
            "BANK NIFTY 57100 PE",
        )

    def test_s9_delta_prefers_selected_contract_provider_response(self) -> None:
        contract = OptionContract(
            security_id="12345",
            strike=57100.0,
            option_type="PE",
            ltp=100.0,
            oi=1000.0,
            oi_change=10.0,
            volume=500.0,
            delta=-0.75,
        )
        self.engine._cache.get_json = lambda *_: (_ for _ in ()).throw(AssertionError("cache should not be read"))
        check = self.engine._check_delta_option("BANK NIFTY 57100 PE", "PE", contract)
        self.assertTrue(check["passed"])
        self.assertEqual(check["data"]["source"], "provider_response")
        self.assertEqual(check["data"]["delta"], -0.75)

    def test_s9_delta_cache_fallback_when_contract_delta_missing(self) -> None:
        contract = OptionContract(
            security_id="12345",
            strike=57100.0,
            option_type="PE",
            ltp=100.0,
            oi=1000.0,
            oi_change=10.0,
            volume=500.0,
        )
        self.engine._cache.get_json = lambda *_: {"delta": -0.74, "provider": "redis"}
        check = self.engine._check_delta_option("BANK NIFTY 57100 PE", "PE", contract)
        self.assertTrue(check["passed"])
        self.assertEqual(check["data"]["source"], "cache")
        self.assertEqual(check["data"]["delta"], -0.74)
        self.assertEqual(check["data"]["cache_source_metadata"]["provider"], "redis")

    def test_s9_delta_missing_is_unavailable_not_out_of_range(self) -> None:
        self.engine._cache.get_json = lambda *_: None
        check = self.engine._check_delta_option("BANK NIFTY 57100 PE", "PE")
        status = self.engine._s9_filter_status("delta", check, "bearish")
        self.assertFalse(check["passed"])
        self.assertEqual(check["reason"], "delta_missing")
        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(status["data"]["lookup_key"], "option:delta:BANK NIFTY 57100 PE:5m")

    def test_s9_scans_alternate_strike_when_nearest_atm_fails(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda symbol, *_: {
            "passed": "56100" in symbol,
            "reason": "ok" if "56100" in symbol else "forced failure",
            "data": {"delta": 0.75 if "56100" in symbol else 0.60},
        }
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        payload = rows[0].payload
        self.assertEqual(rows[0].signal, "STRONG_BUY_CALL")
        self.assertEqual(payload["selected_strike"], 56100)
        self.assertEqual(payload["fully_matched_contract_count"], 1)
        self.assertTrue(payload["signal_only"])
        self.assertFalse(payload["execution_allowed"])
        self.assertEqual(payload["strike_scan"]["total_scanned"], 2)
        self.assertEqual(payload["strike_scan"]["atm_strike"], 56000)
        self.assertEqual(payload["strike_scan"]["lower_range"], 56000)
        self.assertEqual(payload["strike_scan"]["upper_range"], 56100)
        self.assertEqual(payload["strike_scan"]["strike_step"], 100)
        self.assertEqual(payload["strike_scan"]["best_strike"], 56100)
        self.assertIsNone(payload["strike_scan"]["temporary_strike"])
        self.assertEqual([row["strike"] for row in payload["scanned_strikes"]], [56000, 56100])
        selected_row = next(row for row in payload["scanned_strikes"] if row["strike"] == 56100)
        self.assertTrue(selected_row["is_selected"])
        self.assertTrue(selected_row["is_best"])
        self.assertEqual(selected_row["required_filter_count"], 7)
        self.assertEqual(len(selected_row["filters"]), 7)

    def test_s9_multiple_full_matches_nearest_atm_wins(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(rows[0].payload["selected_strike"], 56000)
        self.assertEqual(rows[0].payload["selection_reason"], "FULL_MATCH_NEAREST_ATM")

    def test_s9_partial_best_match_does_not_select_strike(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda symbol, *_: {"passed": False, "reason": "forced delta", "data": {}}
        engine._check_theta_option = lambda symbol, *_: {
            "passed": "56100" in symbol,
            "reason": "ok" if "56100" in symbol else "forced theta",
            "data": {},
        }
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        payload = rows[0].payload
        self.assertIsNone(payload["selected_strike"])
        self.assertIsNone(payload["selected_option_symbol"])
        self.assertIsNone(payload["selected_contract_evaluation"])
        self.assertFalse(payload["confirmed"])
        self.assertIsNone(payload["signal"])
        self.assertEqual(payload["rejection_reason"], "NO_STRIKE_PASSED_ALL_FILTERS")
        self.assertEqual(payload["passed_count"], 6)
        self.assertEqual(payload["filters"]["theta"]["status"], "pass")
        self.assertEqual(payload["strike_scan"]["best_strike"], 56100)
        self.assertEqual(payload["strike_scan"]["temporary_strike"], 56100)
        self.assertIsNone(payload["strike_scan"]["selected_strike"])
        best_row = next(row for row in payload["scanned_strikes"] if row["strike"] == 56100)
        self.assertTrue(best_row["is_best"])
        self.assertFalse(best_row["is_selected"])
        self.assertEqual(best_row["passed_filter_count"], 6)
        self.assertEqual(best_row["first_failed_filter"], "delta")
        self.assertEqual(best_row["rejection_reason"], "forced delta")

    def test_s9_no_full_match_tie_still_does_not_select_strike(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._check_delta_option = lambda *_: {"passed": False, "reason": "forced delta", "data": {}}
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56100, "CE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertIsNone(rows[0].payload["selected_strike"])
        self.assertEqual(rows[0].payload["rejection_reason"], "NO_STRIKE_PASSED_ALL_FILTERS")

    def test_s9_missing_contract_greeks_remain_unavailable(self) -> None:
        engine = self.engine
        engine._cache.get_json = lambda key: {"vwap": 90.0} if key.startswith("vwap:") else None
        engine._check_sweep_buy = lambda *_: {"passed": True, "reason": "ok", "data": {}}
        engine._check_pcr_buy = lambda *_: {"passed": True, "reason": "ok", "data": {}}
        engine._check_pcr_shift_buy = lambda *_: {"passed": True, "reason": "ok", "data": {}}
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE", delta=None, theta=-15.0),), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        delta_filter = rows[0].payload["scanned_contract_evaluations"][0]["filters"]["delta"]
        self.assertEqual(delta_filter["status"], "unavailable")
        self.assertEqual(delta_filter["reason"], "DELTA_CACHE_MISS")
        self.assertIsNone(rows[0].payload["selected_strike"])

    def test_s9_bullish_scans_only_ce_contracts(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        scanned: list[str] = []
        engine._check_delta_option = lambda symbol, *_: scanned.append(symbol) or {"passed": True, "reason": "ok", "data": {}}
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(rows[0].payload["selected_option_type"], "CE")
        self.assertTrue(scanned)
        self.assertTrue(all(symbol.endswith(" CE") for symbol in scanned))

    def test_s9_bearish_scans_only_pe_contracts(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        scanned: list[str] = []
        engine._check_delta_option = lambda symbol, *_: scanned.append(symbol) or {"passed": True, "reason": "ok", "data": {}}
        rows = engine._run_s9(
            states_by_timeframe=self._bearish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertEqual(rows[0].payload["selected_option_type"], "PE")
        self.assertTrue(scanned)
        self.assertTrue(all(symbol.endswith(" PE") for symbol in scanned))

    def test_s9_30m_bullish_15m_bullish_scans_ce_only(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._mixed_index_states(thirty="bullish", fifteen="bullish"),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].payload["effective_direction"], "bullish")
        self.assertEqual(scanned_sides, ["CE"])

    def test_nifty_50_bypasses_trend_gate_and_scans_ce_and_pe(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            underlying_symbol="NIFTY 50",
            nifty_index_symbol="NIFTY 50",
        )
        engine = ScreenerEngine(settings)
        scanned_sides: list[str] = []
        engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = engine._run_s9(
            states_by_timeframe={},
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(scanned_sides, ["CE", "PE"])
        self.assertEqual(rows[0].payload["direction_source"], "direct_option_chain")
        self.assertEqual(rows[0].payload["effective_direction"], "both")
        self.assertNotEqual(rows[0].payload["rejection_reason"], "NIFTY_50_NEUTRAL")

    def test_sensex_bypasses_trend_gate_and_scans_ce_and_pe(self) -> None:
        settings = Settings(
            database_url="sqlite:///./test.db",
            underlying_symbol="SENSEX",
            nifty_index_symbol="SENSEX",
        )
        engine = ScreenerEngine(settings)
        scanned_sides: list[str] = []
        engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = engine._run_s9(
            states_by_timeframe={},
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(scanned_sides, ["CE", "PE"])
        self.assertEqual(rows[0].payload["direction_source"], "direct_option_chain")
        self.assertEqual(rows[0].payload["effective_direction"], "both")
        self.assertNotEqual(rows[0].payload["rejection_reason"], "SENSEX_NEUTRAL")

    def test_s9_30m_bearish_15m_bearish_scans_pe_only(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._mixed_index_states(thirty="bearish", fifteen="bearish"),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].payload["effective_direction"], "bearish")
        self.assertEqual(scanned_sides, ["PE"])

    def test_s9_30m_bullish_15m_bearish_does_not_scan(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._mixed_index_states(thirty="bullish", fifteen="bearish"),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["effective_direction"], "neutral")
        self.assertEqual(scanned_sides, [])

    def test_s9_30m_bearish_15m_bullish_does_not_scan(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._mixed_index_states(thirty="bearish", fifteen="bullish"),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["effective_direction"], "neutral")
        self.assertEqual(scanned_sides, [])

    def test_s9_30m_or_15m_neutral_does_not_scan(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._mixed_index_states(thirty="neutral", fifteen="bullish"),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["effective_direction"], "neutral")
        self.assertEqual(scanned_sides, [])

    def test_s9_missing_30m_or_15m_does_not_scan(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()
        states = self._bullish_index_states()
        states.pop("30m")

        rows = self.engine._run_s9(
            states_by_timeframe=states,
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["effective_direction"], "neutral")
        self.assertEqual(scanned_sides, [])

    def test_s9_missing_ema9_does_not_scan(self) -> None:
        scanned_sides: list[str] = []
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: scanned_sides.append(option_type) or ()
        states = self._bullish_index_states()
        state_15m = states["15m"]["BANK NIFTY"]
        missing_ema_point = _point(
            close=state_15m.latest.close,
            mcginley=100,
            rsi=90,
            macd=10,
            macd_signal=-10,
            ema9=None,
        )
        states["15m"]["BANK NIFTY"] = _state("15m", state_15m.previous, missing_ema_point)

        rows = self.engine._run_s9(
            states_by_timeframe=states,
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].signal, "neutral")
        self.assertEqual(rows[0].payload["effective_direction"], "neutral")
        self.assertEqual(scanned_sides, [])

    def test_s9_confirmed_direction_uses_existing_3m_filter_state_afterward(self) -> None:
        seen_timeframes: list[str] = []
        self.engine._check_index_sweep_buy = lambda state: seen_timeframes.append(state.timeframe) or {
            "passed": False,
            "reason": "forced stop",
            "data": {"timeframe": state.timeframe},
        }
        self.engine._eligible_s9_contracts = lambda _chain, option_type, _mode: ()

        rows = self.engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"), _contract(56000, "PE")), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )

        self.assertEqual(rows[0].payload["effective_direction"], "bullish")
        self.assertEqual(seen_timeframes, ["3m"])

    def test_s9_signal_only_does_not_call_broker_execution_functions(self) -> None:
        engine = self.engine
        self._allow_all_filters(engine)
        engine._dhan.get_option_chain = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker option-chain fetch should not be called by S9 evaluation")
        )
        engine._dhan.get_intraday_ohlc = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker intraday/order-style call should not be called by S9 evaluation")
        )
        rows = engine._run_s9(
            states_by_timeframe=self._bullish_index_states(),
            s1_signals=[],
            option_chain=_option_chain_many((_contract(56000, "CE"),), atm=56000),
            now_market=datetime(2026, 5, 1, 9, 30, 0),
        )
        self.assertTrue(rows[0].payload["signal_only"])
        self.assertFalse(rows[0].payload["execution_allowed"])


if __name__ == "__main__":
    unittest.main()
