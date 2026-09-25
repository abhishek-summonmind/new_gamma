from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db import Base
from backend.app.models import RefreshRun, S9MLSnapshot
from backend.app.services.indicator_engine import SymbolIndicatorState
from backend.app.services.option_types import OptionChainSnapshot, OptionContract
from backend.app.services.s9_ml_snapshot_service import S9MLSnapshotService
from backend.app.services.screener_engine import ScreenerSignal
from backend.app.utils.indicators import IndicatorPoint


def point(at: datetime, close: float) -> IndicatorPoint:
    return IndicatorPoint(
        candle_time=at,
        open=close - 1,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=1500,
        volume_ma=1000,
        volume_ma20=1000,
        rsi=61,
        macd=2,
        macd_signal=1,
        macd_histogram=1,
        ema9=close - 0.5,
        ema20=close - 1,
        vwap=close - 0.25,
        stoch_rsi_k=70,
        stoch_rsi_d=60,
        supertrend=close - 3,
        supertrend_direction="up",
    )


def filter_row(key: str, **data):
    return {
        "key": key,
        "name": key,
        "passed": True,
        "status": "pass",
        "actual_value": next(iter(data.values()), None),
        "data": data,
    }


def signal() -> ScreenerSignal:
    filters = [
        filter_row("pcr_shift", current_volume_pcr=1.1, pcr_shift=0.04),
        filter_row("stoch_rsi", stoch_rsi_k=70, stoch_rsi_d=60),
        filter_row("supertrend", supertrend=98, supertrend_direction="up"),
        filter_row("vwap", vwap=99, premium=100, extension_pct=1.01),
        filter_row("volume_breakout", volume=1500, avg_volume=1000, volume_ratio=1.5),
        filter_row("gamma", gamma=0.045),
        filter_row("delta", delta=0.6),
        filter_row("theta", theta=-0.04),
        filter_row("vega_vix", vega=0.2, india_vix=17.0),
        filter_row("order_book", bid_imbalance=0.56),
        filter_row("spread", spread=0.04),
        filter_row("ema9", close=100, ema9=99.5),
    ]
    candidate = {
        "strike": 25000,
        "option_type": "CE",
        "option_symbol": "NIFTY 50 25000 CE",
        "security_id": "12345",
        "ltp": 100,
        "score": 100,
        "max_score": 100,
        "passed_filter_count": 12,
        "required_filter_count": 12,
        "all_filters_passed": True,
        "filters": filters,
    }
    evaluation = {
        "strike": 25000,
        "option_type": "CE",
        "option_symbol": "NIFTY 50 25000 CE",
        "premium_rsi": 58,
        "score": 100,
        "max_score": 100,
        "raw_score": 100,
        "raw_max_score": 100,
        "passed_count": 12,
        "total_filters": 12,
        "confirmed": True,
        "filters": {item["key"]: item for item in filters},
    }
    return ScreenerSignal(
        screener="S9",
        symbol="NIFTY 50",
        signal="BUY_CALL",
        confidence=1.0,
        reason="S9_FILTER_RANKED_MATCH",
        payload={
            "effective_direction": "bullish",
            "effective_signal": "buy",
            "scanned_strikes": [candidate],
            "scanned_contract_evaluations": [evaluation],
        },
    )


def test_s9_ml_snapshot_is_append_only_deduplicated_and_does_not_mutate_signal():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    service = S9MLSnapshotService(settings)
    now = datetime(2026, 9, 23, 10, 3)
    underlying_state = SymbolIndicatorState("NIFTY 50", "3m", point(now, 25020), None)
    option_state = SymbolIndicatorState("NIFTY 50 25000 CE", "3m", point(now, 100), None)
    contract = OptionContract(
        security_id="12345",
        strike=25000,
        option_type="CE",
        ltp=100,
        oi=50000,
        oi_change=7.5,
        volume=12000,
        delta=0.6,
        theta=-0.04,
        bid_price=99.98,
        ask_price=100.02,
        bid_qty=56,
        ask_qty=44,
        vega=0.2,
        gamma=0.045,
    )
    chain = OptionChainSnapshot(
        underlying="NIFTY 50",
        spot_price=25020,
        atm_strike=25000,
        expiry_date=date(2026, 9, 24),
        snapshot_time=now,
        contracts=(contract,),
    )
    s9_signal = signal()
    original = deepcopy(s9_signal)

    with Session(engine) as db:
        run = RefreshRun(trigger="test", status="running")
        db.add(run)
        db.flush()
        kwargs = {
            "db": db,
            "run_id": run.id,
            "signals": [s9_signal],
            "option_chain": chain,
            "states_by_timeframe": {"3m": {"NIFTY 50": underlying_state}},
            "option_states": {(25000.0, "CE"): option_state},
            "macro_context": {"india_vix": {"value": 17.0}},
        }
        assert service.persist(scan_time=now, **kwargs) == 1
        db.flush()
        assert service.persist(scan_time=now + timedelta(minutes=1), **kwargs) == 0
        assert service.persist(scan_time=now + timedelta(minutes=3), **kwargs) == 1
        db.commit()

        rows = list(db.scalars(select(S9MLSnapshot).order_by(S9MLSnapshot.interval_start)))
        assert len(rows) == 2
        first = rows[0]
        assert first.delta == 0.6
        assert first.gamma == 0.045
        assert first.open_interest == 50000
        assert first.pcr == 1.1
        assert first.pcr_shift == 0.04
        assert first.stoch_rsi_k == 70
        assert first.option_ltp == 100
        assert first.filter_passes["spread"] is True
        assert first.macro_values["india_vix"]["value"] == 17.0

    assert s9_signal == original


def test_unavailable_values_are_stored_as_null():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = S9MLSnapshotService(Settings(database_url="sqlite+pysqlite:///:memory:"))
    now = datetime(2026, 9, 23, 10, 6)
    s9_signal = signal()
    s9_signal.payload["scanned_strikes"][0]["filters"] = []
    s9_signal.payload["scanned_contract_evaluations"] = []
    chain = OptionChainSnapshot(
        underlying="NIFTY 50",
        spot_price=25020,
        atm_strike=25000,
        expiry_date=date(2026, 9, 24),
        snapshot_time=now,
        contracts=(),
    )
    with Session(engine) as db:
        run = RefreshRun(trigger="test", status="running")
        db.add(run)
        db.flush()
        assert service.persist(
            db=db,
            run_id=run.id,
            signals=[s9_signal],
            option_chain=chain,
            states_by_timeframe={},
            option_states={},
            macro_context=None,
            scan_time=now,
        ) == 1
        db.flush()
        row = db.scalar(select(S9MLSnapshot))
        assert row is not None
        assert row.gamma is None
        assert row.open_interest is None
        assert row.bid_qty is None
        assert row.open_price is None


class S9MLSnapshotServiceTests(unittest.TestCase):
    def test_append_only_deduplication_and_signal_immutability(self):
        test_s9_ml_snapshot_is_append_only_deduplicated_and_does_not_mutate_signal()

    def test_unavailable_values_remain_null(self):
        test_unavailable_values_are_stored_as_null()
