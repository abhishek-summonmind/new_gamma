import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.config import Settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import AutoTrade  # noqa: E402
from app.services.auto_trade_service import AutoTradeService  # noqa: E402
from app.services.groww_broker_service import BrokerOrder  # noqa: E402


class FakeBroker:
    def __init__(self, *, lot_size: int = 50, tick_size: float = 0.05, buy_allowed: bool = True, sell_allowed: bool = True):
        self.lot_size = lot_size
        self.tick_size = tick_size
        self.buy_allowed = buy_allowed
        self.sell_allowed = sell_allowed
        self.market_orders: list[dict] = []
        self.stop_orders: list[dict] = []
        self.orders_by_id: dict[str, BrokerOrder] = {}
        self.expiry_by_date: dict[date, date] = {}
        self.resolved_expiries: list[date] = []

    def resolve_option(self, *, underlying, expiry, strike, option_type):
        self.resolved_expiries.append(expiry)
        return {
            "underlying": underlying,
            "trading_symbol": f"{underlying}{expiry:%d%b%y}{int(strike)}{option_type}".upper(),
            "security_id": f"SEC-{underlying}-{int(strike)}-{option_type}",
            "exchange": "NSE",
            "segment": "FNO",
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "buy_allowed": self.buy_allowed,
            "sell_allowed": self.sell_allowed,
        }

    def resolve_stock_option_expiry(self, *, underlying, today):
        return self.expiry_by_date.get(today, date(2026, 9, 24))

    def place_market_order(self, **kwargs):
        self.market_orders.append(kwargs)
        side = kwargs["transaction_type"]
        order_id = "LIVEBUY1" if side == "BUY" else "LIVESELL1"
        return BrokerOrder(order_id, "FILLED", kwargs["quantity"], kwargs["quantity"], 100.0)

    def place_stop_order(self, **kwargs):
        self.stop_orders.append(kwargs)
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def modify_stop(self, **kwargs):
        self.stop_orders.append(kwargs)
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def cancel_order(self, order_id):
        return BrokerOrder(order_id, "CANCELLED", 0, 0, None)

    def get_order(self, order_id):
        if order_id in self.orders_by_id:
            return self.orders_by_id[order_id]
        return BrokerOrder(order_id, "FILLED", self.lot_size, self.lot_size, 100.0)

    def get_order_by_reference(self, _reference_id):
        return None


def point(at: datetime, *, close: float, low: float, high: float, ema9: float):
    return SimpleNamespace(
        candle_time=at,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        volume_ma=90.0,
        rsi=55.0,
        macd=1.0,
        macd_signal=0.5,
        macd_histogram=0.5,
        ema9=ema9,
        mcginley=None,
        sma=None,
        bias="bullish",
        strength=0.8,
    )


def state(symbol: str, at: datetime, *, close: float, low: float, high: float, ema9: float = 100.0):
    return SimpleNamespace(
        symbol=symbol,
        timeframe="3m",
        latest=point(at, close=close, low=low, high=high, ema9=ema9),
        previous=None,
    )


def chain(symbol: str, ltp: float, *, strike: float = 1500.0, option_type: str = "CE"):
    return SimpleNamespace(
        underlying=symbol,
        spot_price=1500.0,
        atm_strike=strike,
        expiry_date=date(2026, 9, 24),
        snapshot_time=datetime(2026, 9, 14, 9, 15),
        contracts=(
            SimpleNamespace(
                security_id=f"OPT-{int(strike)}-{option_type}",
                strike=strike,
                option_type=option_type,
                ltp=ltp,
                oi=1000.0,
                oi_change=10.0,
                volume=500.0,
            ),
        ),
    )


def signal(symbol: str, score: float, *, strike: float = 1500, option_type: str = "CE"):
    is_call = option_type == "CE"
    return SimpleNamespace(
        screener="S9",
        symbol=symbol,
        signal="BUY_CALL" if is_call else "BUY_PUT",
        confidence=score / 100.0,
        reason="test",
        payload={
            "underlying_symbol": symbol,
            "score": score,
            "confirmed": True,
            "execution_allowed": True,
            "entry_rule_version": "S9_ENTRY_V1",
            "signal": "buy_call" if is_call else "buy_put",
            "selected_strike": strike,
            "selected_option_type": option_type,
            "selected_option_symbol": f"{symbol}-{strike}-{option_type}",
            "signal_time": "2026-09-14T09:15:00",
        },
    )


class TestAutoTradeService(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.settings = Settings(
            database_url="sqlite://",
            underlying_symbol="DIXON",
            nifty_index_symbol="DIXON",
            auto_entry_enabled=True,
            auto_entry_dry_run=True,
            auto_entry_stock_min_score=85,
            auto_entry_index_min_score=70,
            auto_entry_hard_stop_pct=10,
            auto_entry_add_trigger_pct=10,
            auto_entry_min_hold_candles=5,
        )
        self.broker = FakeBroker(lot_size=1)
        self.service = AutoTradeService(self.settings, broker=self.broker)
        self.start = datetime(2026, 9, 14, 9, 15)

    def process(self, db, *, score=85, ltp=100, at=None, chart=None, run_id=1, strike=1500, option_type="CE"):
        at = at or self.start
        chart = chart or state("DIXON", at, close=105, low=104, high=106)
        return self.service.process(
            db=db,
            run_id=run_id,
            signals=[signal("DIXON", score, strike=strike, option_type=option_type)],
            option_chain=chain("DIXON", ltp, strike=strike, option_type=option_type),
            states_by_timeframe={"3m": {"DIXON": chart}},
            now_market=at,
        )

    def test_stock_score_gate_rejects_84_and_accepts_85(self):
        with self.Session() as db:
            self.assertEqual(self.process(db, score=84)["entries"], 0)
            self.assertEqual(self.process(db, score=85)["entries"], 1)
            db.commit()
            trade = db.scalar(select(AutoTrade))
            self.assertEqual(trade.status, "OPEN")
            self.assertEqual(trade.hard_stop_price, 90.0)
            self.assertEqual(trade.details["eligibility_snapshot"]["score"], 85.0)
            self.assertEqual(trade.details["eligibility_snapshot"]["minimum_required_score"], 85.0)
            self.assertEqual(trade.details["eligibility_snapshot"]["direction"], "BUY_CALL")

    def test_missing_call_put_direction_cannot_enter_even_with_high_score(self):
        rejected = signal("DIXON", 100)
        rejected.signal = "neutral"
        rejected.payload["signal"] = None
        rejected.payload["selected_option_type"] = None
        with self.Session() as db:
            result = self.service.process(
                db=db,
                run_id=1,
                signals=[rejected],
                option_chain=chain("DIXON", 100),
                states_by_timeframe={"3m": {"DIXON": state("DIXON", self.start, close=105, low=104, high=106)}},
                now_market=self.start,
            )
            self.assertEqual(result["entries"], 0)
            self.assertEqual(result["entry_rejections"][0]["reason"], "SIGNAL_NOT_BUY_CALL_OR_BUY_PUT")

    def test_legacy_macro_risk_payload_no_longer_controls_entry(self):
        candidate = signal("DIXON", 100)
        candidate.payload["macro_effects"] = {"vix_risk_filter_active": True}
        with self.Session() as db:
            result = self.service.process(
                db=db, run_id=1, signals=[candidate], option_chain=chain("DIXON", 100),
                states_by_timeframe={"3m": {"DIXON": state("DIXON", self.start, close=105, low=104, high=106)}},
                now_market=self.start,
            )
            self.assertEqual(result["entries"], 1)

    def test_banknifty_score_gate_rejects_79_and_accepts_80(self):
        settings = self.settings.model_copy(
            update={"underlying_symbol": "BANK NIFTY", "nifty_index_symbol": "BANK NIFTY"}
        )
        service = AutoTradeService(settings, broker=FakeBroker(lot_size=1))
        chart = state("BANK NIFTY", self.start, close=105, low=104, high=106)
        with self.Session() as db:
            rejected = service.process(
                db=db,
                run_id=1,
                signals=[signal("BANK NIFTY", 79)],
                option_chain=chain("BANK NIFTY", 100),
                states_by_timeframe={"3m": {"BANK NIFTY": chart}},
                now_market=self.start,
            )
            accepted = service.process(
                db=db,
                run_id=2,
                signals=[signal("BANK NIFTY", 80)],
                option_chain=chain("BANK NIFTY", 100),
                states_by_timeframe={"3m": {"BANK NIFTY": chart}},
                now_market=self.start,
            )
            self.assertEqual(rejected["entries"], 0)
            self.assertEqual(accepted["entries"], 1)

    def test_legacy_entry_payload_is_hard_blocked(self):
        legacy = signal("DIXON", 100)
        legacy.payload.pop("entry_rule_version")
        with self.Session() as db:
            result = self.service.process(
                db=db,
                run_id=1,
                signals=[legacy],
                option_chain=chain("DIXON", 100),
                states_by_timeframe={"3m": {"DIXON": state("DIXON", self.start, close=105, low=104, high=106)}},
                now_market=self.start,
            )
            self.assertEqual(result["entries"], 0)
            self.assertEqual(result["entry_rejections"][0]["reason"], "LEGACY_ENTRY_LOGIC_DISABLED")

    def test_nifty_and_sensex_legacy_auto_entry_scope_is_disabled(self):
        for symbol_name in ("NIFTY 50", "SENSEX"):
            settings = self.settings.model_copy(
                update={"underlying_symbol": symbol_name, "nifty_index_symbol": symbol_name}
            )
            service = AutoTradeService(settings, broker=FakeBroker(lot_size=1))
            candidate = signal(symbol_name, 100)
            self.assertIsNone(service._entry_candidate(signals=[candidate], symbol=symbol_name))
            self.assertEqual(
                service._entry_candidate_rejection_reason(signals=[candidate], symbol=symbol_name),
                "ENTRY_SCOPE_UNSUPPORTED",
            )

    def test_add_lot_flow_is_disabled_but_opposite_ema_close_does_not_exit(self):
        with self.Session() as db:
            self.process(db)
            db.commit()

            at = self.start + timedelta(minutes=3)
            retest = state("DIXON", at, close=105, low=99, high=112)
            first_manage = self.process(db, ltp=111, at=at, chart=retest)
            self.assertEqual(first_manage["adds"], 0)
            db.commit()

            for candle_number in range(2, 5):
                at = self.start + timedelta(minutes=3 * candle_number)
                self.process(db, ltp=115, at=at, chart=state("DIXON", at, close=106, low=104, high=108))
                db.commit()

            at = self.start + timedelta(minutes=15)
            result = self.process(db, ltp=120, at=at, chart=state("DIXON", at, close=99, low=98, high=101))
            db.commit()

            trade = db.scalar(select(AutoTrade))
            self.assertEqual(result["exits"], 0)
            self.assertFalse(trade.add_lot_armed)
            self.assertFalse(trade.add_lot_used)
            self.assertEqual(int(trade.added_quantity or 0), 0)
            self.assertTrue(trade.trailing_active)
            self.assertEqual(trade.hold_completed_candles, 5)
            self.assertEqual(trade.status, "OPEN")
            self.assertIsNone(trade.exit_reason)

    def test_hard_stop_exits_before_minimum_hold(self):
        with self.Session() as db:
            self.process(db)
            db.commit()
            at = self.start + timedelta(minutes=3)
            result = self.process(db, ltp=89, at=at)
            db.commit()
            trade = db.scalar(select(AutoTrade))
            self.assertEqual(result["exits"], 1)
            self.assertEqual(trade.hold_completed_candles, 0)
            self.assertEqual(trade.exit_reason, "HARD_STOP_LOSS")

    def test_trailing_activation_breakeven_and_max_high_exit_use_ticks(self):
        with self.Session() as db:
            self.process(db)
            db.commit()

            at = self.start + timedelta(minutes=1)
            self.process(db, ltp=108, at=at)
            trade = db.scalar(select(AutoTrade))
            self.assertTrue(trade.breakeven_active)
            self.assertFalse(trade.trailing_active)
            self.assertIsNone(trade.max_high)
            self.assertEqual(trade.hard_stop_price, 100.0)

            at = self.start + timedelta(minutes=2)
            self.process(db, ltp=115, at=at)
            self.assertTrue(trade.trailing_active)
            self.assertEqual(trade.max_high, 115.0)
            self.assertEqual(trade.hard_stop_price, 100.0)

            at = self.start + timedelta(minutes=3)
            self.process(db, ltp=116, at=at)
            self.assertEqual(trade.max_high, 116.0)

            at = self.start + timedelta(minutes=4)
            result = self.process(db, ltp=113, at=at)
            self.assertEqual(result["exits"], 1)
            self.assertEqual(trade.exit_reason, "TRAILING_MAX_HIGH_MINUS_3")
            self.assertLess(trade.hold_completed_candles, self.settings.auto_entry_min_hold_candles)

    def test_breakeven_exit_uses_live_ticks_without_candle_wait(self):
        with self.Session() as db:
            self.process(db)
            db.commit()

            self.process(db, ltp=108, at=self.start + timedelta(minutes=1))
            result = self.process(db, ltp=100, at=self.start + timedelta(minutes=2))
            trade = db.scalar(select(AutoTrade))

            self.assertEqual(result["exits"], 1)
            self.assertEqual(trade.exit_reason, "BREAKEVEN_STOP")
            self.assertLess(trade.hold_completed_candles, self.settings.auto_entry_min_hold_candles)

    def test_stock_entry_keeps_current_month_expiry_after_16th(self):
        self.broker.expiry_by_date = {
            date(2026, 9, 16): date(2026, 9, 24),
            date(2026, 9, 17): date(2026, 9, 24),
        }
        with self.Session() as db:
            first = self.process(db, at=datetime(2026, 9, 16, 9, 15), run_id=1)
            first_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 1))
            AutoTradeService._close_trade(
                trade=first_trade,
                price=101,
                reason="TEST_EXIT",
                now_market=datetime(2026, 9, 16, 9, 30),
            )
            second = self.process(db, at=datetime(2026, 9, 17, 9, 15), run_id=2)
            second_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 2))

            self.assertEqual(first["entries"], 1)
            self.assertEqual(second["entries"], 1)
            self.assertEqual(first_trade.expiry_date, date(2026, 9, 24))
            self.assertEqual(second_trade.expiry_date, date(2026, 9, 24))
            self.assertEqual(self.broker.resolved_expiries[-2:], [date(2026, 9, 24), date(2026, 9, 24)])

    def test_index_entry_does_not_use_stock_expiry_rollover(self):
        settings = self.settings.model_copy(
            update={"underlying_symbol": "BANK NIFTY", "nifty_index_symbol": "BANK NIFTY"}
        )
        broker = FakeBroker(lot_size=1)
        broker.expiry_by_date = {date(2026, 9, 17): date(2026, 10, 29)}
        service = AutoTradeService(settings, broker=broker)
        chart = state("BANK NIFTY", datetime(2026, 9, 17, 9, 15), close=105, low=104, high=106)
        with self.Session() as db:
            result = service.process(
                db=db,
                run_id=1,
                signals=[signal("BANK NIFTY", 80)],
                option_chain=chain("BANK NIFTY", 100),
                states_by_timeframe={"3m": {"BANK NIFTY": chart}},
                now_market=datetime(2026, 9, 17, 9, 15),
            )
            trade = db.scalar(select(AutoTrade))

            self.assertEqual(result["entries"], 1)
            self.assertEqual(trade.expiry_date, date(2026, 9, 24))
            self.assertEqual(broker.resolved_expiries[-1], date(2026, 9, 24))

    def test_same_day_reentry_waits_for_closed_trade_and_creates_new_identity(self):
        with self.Session() as db:
            first = self.process(db, score=86, ltp=100, run_id=1)
            db.commit()
            first_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 1))
            self.assertEqual(first["entries"], 1)
            self.assertEqual(first_trade.status, "OPEN")

            blocked = self.process(db, score=91, ltp=120, run_id=2)
            self.assertEqual(blocked["entries"], 0)
            self.assertEqual(blocked["entry_rejections"][0]["reason"], "OPEN_POSITION_EXISTS")
            self.assertEqual(db.scalars(select(AutoTrade)).all(), [first_trade])

            AutoTradeService._close_trade(
                trade=first_trade,
                price=95,
                reason="TEST_EXIT",
                now_market=self.start + timedelta(minutes=3),
            )
            db.commit()
            reopened = self.process(
                db,
                score=92,
                ltp=130,
                run_id=3,
                at=self.start + timedelta(minutes=6),
            )
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(reopened["entries"], 1)
            self.assertEqual(len(trades), 2)
            self.assertNotEqual(trades[0].id, trades[1].id)
            self.assertNotEqual(trades[0].entry_order_id, trades[1].entry_order_id)
            self.assertEqual(trades[1].run_id, 3)
            self.assertEqual(trades[1].score, 92.0)
            self.assertEqual(trades[1].average_entry_price, 130.0)
            self.assertEqual(trades[1].hard_stop_price, 117.0)
            self.assertEqual(trades[1].details["eligibility_snapshot"]["score"], 92.0)

    def test_same_day_reentry_allows_different_strike_after_close(self):
        with self.Session() as db:
            self.process(db, strike=1500, option_type="CE", ltp=100, run_id=1)
            first_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 1))
            AutoTradeService._close_trade(
                trade=first_trade,
                price=96,
                reason="TEST_EXIT",
                now_market=self.start + timedelta(minutes=3),
            )
            db.commit()

            result = self.process(db, strike=1550, option_type="CE", ltp=140, run_id=2)
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(result["entries"], 1)
            self.assertEqual([trade.strike for trade in trades], [1500.0, 1550.0])
            self.assertEqual(trades[1].average_entry_price, 140.0)
            self.assertEqual(trades[1].hard_stop_price, 126.0)

    def test_same_day_reentry_allows_call_then_put_after_close(self):
        with self.Session() as db:
            self.process(db, strike=1500, option_type="CE", ltp=100, run_id=1)
            first_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 1))
            AutoTradeService._close_trade(
                trade=first_trade,
                price=96,
                reason="TEST_EXIT",
                now_market=self.start + timedelta(minutes=3),
            )
            db.commit()

            result = self.process(db, strike=1500, option_type="PE", ltp=125, run_id=2)
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(result["entries"], 1)
            self.assertEqual([trade.option_type for trade in trades], ["CE", "PE"])
            self.assertEqual(trades[1].direction, "BEARISH")
            self.assertEqual(trades[1].details["eligibility_snapshot"]["direction"], "BUY_PUT")

    def test_repeated_same_scan_after_close_does_not_duplicate_entry(self):
        with self.Session() as db:
            self.process(db, ltp=100, run_id=1)
            first_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 1))
            AutoTradeService._close_trade(
                trade=first_trade,
                price=95,
                reason="TEST_EXIT",
                now_market=self.start + timedelta(minutes=3),
            )
            db.commit()

            first_retry = self.process(db, ltp=130, run_id=2, at=self.start + timedelta(minutes=6))
            second_trade = db.scalar(select(AutoTrade).where(AutoTrade.run_id == 2))
            AutoTradeService._close_trade(
                trade=second_trade,
                price=129,
                reason="TEST_EXIT",
                now_market=self.start + timedelta(minutes=9),
            )
            second_retry = self.process(db, ltp=131, run_id=2, at=self.start + timedelta(minutes=9))
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(first_retry["entries"], 1)
            self.assertEqual(second_retry["entries"], 0)
            self.assertEqual(second_retry["entry_rejections"][0]["reason"], "ENTRY_ALREADY_SUBMITTED")
            self.assertEqual(len(trades), 2)

    def test_live_closed_trade_must_be_reconciled_before_reentry(self):
        live_settings = self.settings.model_copy(update={"auto_entry_dry_run": False})
        live_broker = FakeBroker(lot_size=1)
        live_service = AutoTradeService(live_settings, broker=live_broker)
        with self.Session() as db:
            trade = AutoTrade(
                run_id=1,
                symbol="DIXON",
                option_symbol="DIXON24SEP261500CE",
                security_id="SEC-DIXON-1500-CE",
                exchange="NSE",
                segment="FNO",
                lot_size=1,
                tick_size=0.05,
                expiry_date=date(2026, 9, 24),
                strike=1500,
                option_type="CE",
                direction="BULLISH",
                score=86,
                status="CLOSED",
                dry_run=False,
                management_state="CLOSED",
                entry_order_id="BUY1",
                entry_order_status="FILLED",
                initial_quantity=1,
                filled_quantity=1,
                open_quantity=0,
                average_entry_price=100,
                initial_average_price=100,
                hard_stop_price=90,
                exit_order_id="SELL1",
                exit_order_status="OPEN",
                exit_requested_quantity=1,
                exit_filled_quantity=1,
                exit_price=95,
                exit_reason="TEST_EXIT",
                created_at=self.start,
                updated_at=self.start,
            )
            db.add(trade)
            db.commit()
            live_broker.orders_by_id["SELL1"] = BrokerOrder("SELL1", "OPEN", 1, 0, None)

            blocked = live_service.process(
                db=db,
                run_id=2,
                signals=[signal("DIXON", 90)],
                option_chain=chain("DIXON", 130),
                states_by_timeframe={"3m": {"DIXON": state("DIXON", self.start, close=105, low=104, high=106)}},
                now_market=self.start + timedelta(minutes=3),
            )
            self.assertEqual(blocked["entries"], 0)
            self.assertEqual(blocked["entry_rejections"][0]["reason"], "OPEN_POSITION_EXISTS")
            self.assertEqual(len(live_broker.market_orders), 0)

            trade.exit_order_status = "FILLED"
            trade.last_broker_reconciled_at = self.start + timedelta(minutes=4)
            live_broker.orders_by_id["SELL1"] = BrokerOrder("SELL1", "FILLED", 1, 1, 95.0)
            db.commit()
            allowed = live_service.process(
                db=db,
                run_id=3,
                signals=[signal("DIXON", 91)],
                option_chain=chain("DIXON", 140),
                states_by_timeframe={"3m": {"DIXON": state("DIXON", self.start, close=105, low=104, high=106)}},
                now_market=self.start + timedelta(minutes=6),
            )
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(allowed["entries"], 1)
            self.assertEqual(len(trades), 2)
            self.assertEqual(trades[1].run_id, 3)
            self.assertEqual(trades[1].average_entry_price, 100.0)
            self.assertEqual(trades[1].hard_stop_price, 90.0)

    def test_stale_dry_trade_from_previous_day_does_not_show_active_or_bypass_score_gate(self):
        yesterday = self.start - timedelta(days=1)
        with self.Session() as db:
            trade = AutoTrade(
                run_id=1,
                symbol="DIXON",
                option_symbol="DIXON24SEP261500PE",
                security_id="SEC-DIXON-1500-PE",
                exchange="NSE",
                segment="FNO",
                lot_size=1,
                tick_size=0.05,
                expiry_date=date(2026, 9, 24),
                strike=1500,
                option_type="PE",
                direction="BEARISH",
                score=85,
                status="OPEN",
                dry_run=True,
                management_state="TREND_RIDER",
                entry_order_id="DRY-ENTRY-1-DIXON",
                entry_order_status="FILLED",
                initial_quantity=1,
                filled_quantity=1,
                open_quantity=1,
                average_entry_price=100,
                initial_average_price=100,
                hard_stop_price=90,
                current_ltp=98,
                sl_order_status="DRY_RUN_ACTIVE",
                sl_quantity=1,
                created_at=yesterday,
                updated_at=yesterday,
            )
            db.add(trade)
            db.commit()

            result = self.process(db, score=75, ltp=99, run_id=2, at=self.start)
            db.commit()
            trades = db.scalars(select(AutoTrade).order_by(AutoTrade.id)).all()

            self.assertEqual(result["entries"], 0)
            self.assertEqual(result["exits"], 1)
            self.assertEqual(result["entry_rejections"][0]["reason"], "SCORE_BELOW_MIN_85")
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].status, "CLOSED")
            self.assertEqual(trades[0].exit_reason, "DRY_RUN_DAY_EXPIRED")
            self.assertEqual(trades[0].open_quantity, 0)


if __name__ == "__main__":
    unittest.main()
