from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings
from app.services.option_types import OptionChainSnapshot, OptionContract
from app.services.screener_engine import ScreenerEngine


def _engine() -> ScreenerEngine:
    return ScreenerEngine(Settings(database_url="sqlite:///./test.db", redis_url="memory://"))


def _state(**values):
    defaults = {
        "candle_time": datetime(2026, 9, 26, 10, 0),
        "close": 110.0,
        "ema20": 105.0,
        "ema200": 100.0,
        "rsi": 60.0,
    }
    defaults.update(values)
    return SimpleNamespace(timeframe="test", latest=SimpleNamespace(**defaults))


def _chain(*, when: datetime, ce_oi: float = 100.0, pe_oi: float = 100.0, days: int = 20):
    return OptionChainSnapshot(
        underlying="BANKNIFTY",
        spot_price=50_000.0,
        atm_strike=50_000,
        expiry_date=when.date() + timedelta(days=days),
        snapshot_time=when,
        contracts=(
            OptionContract("ce", 50_000, "CE", 100.0, ce_oi, 0.0, 100.0),
            OptionContract("pe", 50_000, "PE", 100.0, pe_oi, 0.0, 100.0),
        ),
    )


def test_s9_entry_direction_uses_one_hour_ema20_vs_ema200() -> None:
    engine = _engine()
    states = {"s9_60m_completed": {"BANK NIFTY": _state(ema20=110.0, ema200=100.0)}}
    assert engine._resolve_s9_entry_direction(
        states_by_timeframe=states,
        underlying_symbol="BANK NIFTY",
    ) == ("bullish", "buy")


def test_s9_entry_time_rsi_spread_and_expiry_are_hard_gates() -> None:
    engine = _engine()
    assert not engine._check_s9_scan_start(datetime(2026, 9, 26, 9, 24, 59))["passed"]
    assert engine._check_s9_scan_start(datetime(2026, 9, 26, 9, 25, 0))["passed"]
    assert engine._check_s9_daily_rsi(_state(rsi=55.0), "bullish")["passed"]
    assert not engine._check_s9_daily_rsi(_state(rsi=65.01), "bullish")["passed"]

    good = OptionContract("ce", 50_000, "CE", 100.0, 1.0, 0.0, 1.0, bid_price=99.91, ask_price=100.0)
    boundary = OptionContract("ce", 50_000, "CE", 100.0, 1.0, 0.0, 1.0, bid_price=99.90, ask_price=100.0)
    assert engine._check_s9_spread(good)["passed"]
    assert not engine._check_s9_spread(boundary)["passed"]

    now = datetime(2026, 9, 26, 10, 0)
    assert engine._check_s9_expiry_safety(_chain(when=now, days=14), now_market=now)["passed"]
    assert not engine._check_s9_expiry_safety(_chain(when=now, days=13), now_market=now)["passed"]


def test_s9_entry_uses_five_minute_oi_pcr_shift() -> None:
    engine = _engine()
    start = datetime(2026, 9, 26, 10, 0)
    engine._record_s9_entry_pcr(_chain(when=start), now_market=start)
    current = _chain(when=start + timedelta(minutes=5), pe_oi=102.0)
    engine._record_s9_entry_pcr(current, now_market=start + timedelta(minutes=5))
    result = engine._check_s9_entry_pcr_shift(
        current,
        "bullish",
        now_market=start + timedelta(minutes=5),
    )
    assert result["passed"]
    assert result["data"]["pcr_shift"] >= 0.01


def test_s9_entry_order_book_accepts_60_percent_only_after_five_seconds() -> None:
    engine = _engine()
    contract = OptionContract(
        "ce",
        50_000,
        "CE",
        100.0,
        1.0,
        0.0,
        1.0,
        bid_qty=60.0,
        ask_qty=40.0,
    )
    start = datetime(2026, 9, 26, 10, 0)
    result = None
    for second in range(6):
        result = engine._check_s9_order_book(
            contract,
            "bullish",
            option_symbol="BANKNIFTY50000CE",
            now_market=start + timedelta(seconds=second),
        )
    assert result is not None and result["passed"]
