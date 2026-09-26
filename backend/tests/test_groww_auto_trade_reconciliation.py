import unittest
from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import AutoTrade
from app.services.auto_trade_service import AutoTradeService
from app.services.groww_broker_service import BrokerOrder


class FakeBroker:
    def __init__(self):
        self.entry_detail = BrokerOrder("ENTRY1", "FILLED", 50, 50, 101.0)
        self.stop_quantity = 0

    def resolve_option(self, **_kwargs):
        return {
            "trading_symbol": "DIXON26SEP1500CE", "security_id": "66509",
            "exchange": "NSE", "segment": "FNO", "lot_size": 50, "tick_size": 0.05,
            "buy_allowed": True, "sell_allowed": True,
        }

    def place_market_order(self, **kwargs):
        if kwargs["transaction_type"] == "BUY":
            return BrokerOrder("ENTRY1", "OPEN", kwargs["quantity"], 0, None)
        return BrokerOrder("EXIT1", "OPEN", kwargs["quantity"], 0, None)

    def place_stop_order(self, **kwargs):
        self.stop_quantity = kwargs["quantity"]
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def modify_stop(self, **kwargs):
        self.stop_quantity = kwargs["quantity"]
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def cancel_order(self, order_id):
        return BrokerOrder(order_id, "CANCELLED", self.stop_quantity, 0, None)

    def get_order(self, order_id):
        if order_id == "ENTRY1":
            return self.entry_detail
        return BrokerOrder(order_id, "OPEN", self.stop_quantity, 0, None)

    def get_order_by_reference(self, _reference_id):
        return None


class ReadinessBroker:
    def __init__(
        self,
        *,
        lot_size=300,
        tick_size=0.05,
        buy_allowed=True,
        sell_allowed=True,
        resolve_error: Exception | None = None,
        submit_error: Exception | None = None,
        fill_buy=False,
    ):
        self.lot_size = lot_size
        self.tick_size = tick_size
        self.buy_allowed = buy_allowed
        self.sell_allowed = sell_allowed
        self.resolve_error = resolve_error
        self.submit_error = submit_error
        self.fill_buy = fill_buy
        self.market_orders: list[dict] = []
        self.stop_orders: list[dict] = []
        self.cancelled: list[str] = []
        self.orders_by_id: dict[str, BrokerOrder] = {}
        self.orders_by_ref: dict[str, BrokerOrder] = {}

    def resolve_option(self, *, underlying, expiry, strike, option_type):
        if self.resolve_error is not None:
            raise self.resolve_error
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

    def place_market_order(self, **kwargs):
        if self.submit_error is not None:
            raise self.submit_error
        self.market_orders.append(kwargs)
        side = kwargs["transaction_type"]
        order_id = f"{side}{len(self.market_orders)}"
        filled = kwargs["quantity"] if side == "SELL" or (side == "BUY" and self.fill_buy) else 0
        average = 111.0 if side == "SELL" else 100.0 if side == "BUY" and self.fill_buy else None
        order = BrokerOrder(order_id, "OPEN", kwargs["quantity"], filled, average, kwargs["reference_id"])
        self.orders_by_id[order_id] = order
        self.orders_by_ref[kwargs["reference_id"]] = order
        return order

    def place_stop_order(self, **kwargs):
        self.stop_orders.append(kwargs)
        order = BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None, kwargs["reference_id"])
        self.orders_by_id["STOP1"] = order
        return order

    def modify_stop(self, **kwargs):
        self.stop_orders.append(kwargs)
        order = BrokerOrder(kwargs["order_id"], "OPEN", kwargs["quantity"], 0, None)
        self.orders_by_id[kwargs["order_id"]] = order
        return order

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        order = BrokerOrder(order_id, "CANCELLED", 0, 0, None)
        self.orders_by_id[order_id] = order
        return order

    def get_order(self, order_id):
        return self.orders_by_id[order_id]

    def get_order_by_reference(self, reference_id):
        return self.orders_by_ref.get(reference_id)


class CrashingAfterSubmitService(AutoTradeService):
    def _apply_entry_order(self, **kwargs):
        raise RuntimeError("simulated db failure after broker submit")


def make_signal(symbol="HAL", *, option_type="CE", strike=1500, score=85):
    return SimpleNamespace(
        screener="S9",
        symbol=symbol,
        signal="BUY_CALL" if option_type == "CE" else "BUY_PUT",
        confidence=score / 100.0,
        reason="test",
        payload={
            "underlying_symbol": symbol,
            "score": score,
            "confirmed": True,
            "execution_allowed": True,
            "entry_rule_version": "S9_ENTRY_V1",
            "signal": "buy_call" if option_type == "CE" else "buy_put",
            "selected_strike": strike,
            "selected_option_type": option_type,
            "selected_option_symbol": f"{symbol}-{strike}-{option_type}",
            "signal_time": "2026-09-14T09:15:00",
        },
    )


def make_chain(symbol="HAL", *, option_type="CE", strike=1500, ltp=100):
    return SimpleNamespace(
        expiry_date=date(2026, 9, 24),
        contracts=(SimpleNamespace(security_id=f"{symbol}-{strike}-{option_type}", strike=strike, option_type=option_type, ltp=ltp),),
    )


def make_states(symbol="HAL", now=datetime(2026, 9, 14, 9, 15), *, close=105, low=104, high=106, ema9=100):
    latest = SimpleNamespace(candle_time=now, open=close, high=high, low=low, close=close, ema9=ema9)
    return {"3m": {symbol: SimpleNamespace(latest=latest)}}


class TestGrowwAutoTradeReconciliation(unittest.TestCase):
    def test_lot_resolution_pending_recovery_and_fill_based_stop(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(
            database_url="sqlite://", underlying_symbol="DIXON", auto_entry_enabled=True,
            auto_entry_dry_run=False, auto_entry_stock_min_score=85,
            auto_entry_hard_stop_pct=10, auto_entry_lots=1,
        )
        broker = FakeBroker()
        service = AutoTradeService(settings, broker=broker)
        now = datetime(2026, 9, 14, 9, 15)
        signal = SimpleNamespace(symbol="DIXON", signal="BUY_CALL", payload={
            "underlying_symbol": "DIXON", "score": 85, "confirmed": True,
            "execution_allowed": True, "entry_rule_version": "S9_ENTRY_V1",
            "selected_strike": 1500, "selected_option_type": "CE",
            "selected_option_symbol": "DIXON26SEP1500CE",
        })
        contract = SimpleNamespace(security_id="DIXON26SEP1500CE", strike=1500, option_type="CE", ltp=100)
        chain = SimpleNamespace(expiry_date=date(2026, 9, 24), contracts=(contract,))
        latest = SimpleNamespace(candle_time=now, open=105, high=106, low=104, close=105, ema9=100)
        states = {"3m": {"DIXON": SimpleNamespace(latest=latest)}}

        with Session() as db:
            first = service.process(db=db, run_id=1, signals=[signal], option_chain=chain, states_by_timeframe=states, now_market=now)
            db.flush()
            trade = db.scalar(select(AutoTrade))
            self.assertEqual(first["entries"], 1)
            self.assertEqual(trade.status, "ENTRY_PENDING")
            self.assertEqual(trade.initial_quantity, 50)

            service.process(db=db, run_id=2, signals=[signal], option_chain=chain, states_by_timeframe=states, now_market=now)
            self.assertEqual(trade.status, "OPEN")
            self.assertEqual(trade.average_entry_price, 101.0)
            self.assertEqual(trade.hard_stop_price, 90.9)
            self.assertEqual(trade.sl_order_id, "STOP1")
            self.assertEqual(broker.stop_quantity, 50)

    def test_partial_entry_resizes_and_reprices_protective_stop(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="DIXON", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        broker = FakeBroker()
        broker.entry_detail = BrokerOrder("ENTRY1", "OPEN", 50, 25, 100.5)
        service = AutoTradeService(settings, broker=broker)
        now = datetime(2026, 9, 14, 9, 15)
        signal = SimpleNamespace(symbol="DIXON", signal="BUY_CALL", payload={"underlying_symbol": "DIXON", "score": 85,
            "confirmed": True, "execution_allowed": True, "entry_rule_version": "S9_ENTRY_V1",
            "selected_strike": 1500, "selected_option_type": "CE",
            "selected_option_symbol": "DIXON26SEP1500CE"})
        chain = SimpleNamespace(expiry_date=date(2026, 9, 24), contracts=(
            SimpleNamespace(security_id="DIXON26SEP1500CE", strike=1500, option_type="CE", ltp=105),))
        latest = SimpleNamespace(candle_time=now, open=105, high=106, low=104, close=105, ema9=100)
        states = {"3m": {"DIXON": SimpleNamespace(latest=latest)}}
        with Session() as db:
            service.process(db=db, run_id=1, signals=[signal], option_chain=chain, states_by_timeframe=states, now_market=now)
            db.flush()
            service.process(db=db, run_id=2, signals=[signal], option_chain=chain, states_by_timeframe=states, now_market=now)
            trade = db.scalar(select(AutoTrade))
            self.assertEqual((trade.open_quantity, broker.stop_quantity, trade.hard_stop_price), (25, 25, 90.45))

            broker.entry_detail = BrokerOrder("ENTRY1", "FILLED", 50, 50, 101.0)
            service.process(db=db, run_id=3, signals=[signal], option_chain=chain, states_by_timeframe=states, now_market=now)
            self.assertEqual((trade.open_quantity, broker.stop_quantity, trade.hard_stop_price), (50, 50, 90.9))

    def test_hal_dry_run_and_live_resolve_same_exchange_lot_and_quantity(self):
        now = datetime(2026, 9, 14, 9, 15)
        dry_broker = ReadinessBroker(lot_size=300, tick_size=0.05)
        live_broker = ReadinessBroker(lot_size=300, tick_size=0.05)
        dry_settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                                auto_entry_dry_run=True, auto_entry_stock_min_score=85, auto_entry_lots=1)
        live_settings = dry_settings.model_copy(update={"auto_entry_dry_run": False})
        dry_engine = create_engine("sqlite://", poolclass=StaticPool)
        live_engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(dry_engine)
        Base.metadata.create_all(live_engine)
        DrySession = sessionmaker(bind=dry_engine, expire_on_commit=False)
        LiveSession = sessionmaker(bind=live_engine, expire_on_commit=False)
        with DrySession() as db:
            AutoTradeService(dry_settings, broker=dry_broker).process(
                db=db, run_id=1, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            dry_trade = db.scalar(select(AutoTrade).where(AutoTrade.dry_run.is_(True)))
        with LiveSession() as db:
            AutoTradeService(live_settings, broker=live_broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            live_trade = db.scalar(select(AutoTrade).where(AutoTrade.dry_run.is_(False)))

        self.assertEqual(dry_trade.lot_size, 300)
        self.assertEqual(dry_trade.initial_quantity, 300)
        self.assertEqual(live_trade.lot_size, 300)
        self.assertEqual(live_trade.initial_quantity, 300)
        self.assertEqual(dry_trade.exchange, live_trade.exchange)
        self.assertEqual(dry_trade.segment, live_trade.segment)
        self.assertEqual(dry_trade.tick_size, live_trade.tick_size)
        self.assertEqual(dry_trade.option_symbol, live_trade.option_symbol)
        self.assertEqual(dry_trade.security_id, live_trade.security_id)
        self.assertEqual(dry_trade.expiry_date, live_trade.expiry_date)
        self.assertEqual(dry_trade.strike, live_trade.strike)
        self.assertEqual(dry_trade.option_type, live_trade.option_type)
        self.assertEqual(live_broker.market_orders[0]["quantity"], 300)
        self.assertNotEqual(dry_trade.initial_quantity, 1)

    def test_permission_rejection_and_missing_instrument_fail_closed(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        now = datetime(2026, 9, 14, 9, 15)
        with Session() as db:
            buy_blocked = ReadinessBroker(buy_allowed=False)
            result = AutoTradeService(settings, broker=buy_blocked).process(
                db=db, run_id=1, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            self.assertEqual(result["entries"], 0)
            self.assertEqual(result["entry_rejections"][0]["reason"], "GROWW_BUY_NOT_ALLOWED")
            self.assertEqual(buy_blocked.market_orders, [])

            missing = ReadinessBroker(resolve_error=RuntimeError("ambiguous instrument"))
            result = AutoTradeService(settings, broker=missing).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            self.assertEqual(result["entries"], 0)
            self.assertEqual(result["entry_rejections"][0]["reason"], "INSTRUMENT_RESOLUTION_FAILED")
            self.assertEqual(missing.market_orders, [])

            sell_blocked = ReadinessBroker(sell_allowed=False, fill_buy=True)
            AutoTradeService(settings, broker=sell_blocked).process(
                db=db, run_id=3, signals=[make_signal("HAL", strike=1550)], option_chain=make_chain(strike=1550),
                states_by_timeframe=make_states(), now_market=now,
            )
            trade = db.scalar(select(AutoTrade).where(AutoTrade.strike == 1550))
            self.assertEqual(trade.error_code, "GROWW_SELL_NOT_ALLOWED")
            self.assertEqual(sell_blocked.stop_orders, [])

    def test_broker_timeout_persists_intent_and_blocks_duplicate_retry(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        broker = ReadinessBroker(submit_error=TimeoutError("timeout"))
        now = datetime(2026, 9, 14, 9, 15)
        with Session() as db:
            result = AutoTradeService(settings, broker=broker).process(
                db=db, run_id=1, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            trade = db.scalar(select(AutoTrade))
            self.assertEqual(result["entries"], 0)
            self.assertEqual(trade.status, "ENTRY_PENDING")
            self.assertEqual(trade.error_code, "ENTRY_SUBMISSION_AMBIGUOUS")
            broker.submit_error = None
            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            self.assertEqual(len(broker.market_orders), 0)

    def test_restart_recovers_pending_intent_by_reference_after_post_submit_failure(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        broker = ReadinessBroker(lot_size=300)
        now = datetime(2026, 9, 14, 9, 15)
        with Session() as db:
            with self.assertRaises(RuntimeError):
                CrashingAfterSubmitService(settings, broker=broker).process(
                    db=db, run_id=1, signals=[make_signal()], option_chain=make_chain(),
                    states_by_timeframe=make_states(), now_market=now,
                )
        ref = next(iter(broker.orders_by_ref))
        broker.orders_by_ref[ref] = BrokerOrder("BUY1", "FILLED", 300, 300, 101.0, ref)
        broker.orders_by_id["BUY1"] = broker.orders_by_ref[ref]
        with Session() as db:
            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(),
                states_by_timeframe=make_states(), now_market=now,
            )
            trade = db.scalar(select(AutoTrade))
            self.assertEqual(trade.status, "OPEN")
            self.assertEqual(trade.entry_order_id, "BUY1")
            self.assertEqual(trade.open_quantity, 300)
            self.assertEqual(broker.stop_orders[-1]["quantity"], 300)

    def test_add_lot_reconciliation_is_temporarily_disabled(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        broker = ReadinessBroker(lot_size=300)
        now = datetime(2026, 9, 14, 9, 15)
        with Session() as db:
            trade = AutoTrade(
                run_id=1, symbol="HAL", option_symbol="HAL24SEP261500CE", security_id="SEC",
                exchange="NSE", segment="FNO", lot_size=300, tick_size=0.05,
                expiry_date=date(2026, 9, 24), strike=1500, option_type="CE",
                direction="BULLISH", score=85, status="OPEN", dry_run=False,
                management_state="ADD_PENDING", entry_order_id="BUY1", entry_order_status="FILLED",
                initial_quantity=300, filled_quantity=300, open_quantity=300,
                average_entry_price=100, initial_average_price=100, hard_stop_price=90,
                current_ltp=120, sl_order_id="STOP1", sl_order_status="OPEN", sl_quantity=300,
                add_lot_armed=True, add_lot_used=True, add_requested_quantity=300,
                add_order_id="ADD1", add_order_status="OPEN",
                details={
                    "resolved_instrument": {"buy_allowed": True, "sell_allowed": True},
                    "protective_stop": {
                        "order_id": "STOP1",
                        "quantity": 300,
                        "trigger_price": 90.0,
                        "limit_price": 89.95,
                    },
                },
                created_at=now, updated_at=now,
            )
            db.add(trade)
            broker.orders_by_id["BUY1"] = BrokerOrder("BUY1", "FILLED", 300, 300, 100.0)
            broker.orders_by_id["STOP1"] = BrokerOrder("STOP1", "OPEN", 300, 0, None)
            broker.orders_by_id["ADD1"] = BrokerOrder("ADD1", "OPEN", 300, 150, 120.0)
            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(ltp=120),
                states_by_timeframe=make_states(close=120, low=118, high=121, ema9=100), now_market=now,
            )
            self.assertEqual(trade.open_quantity, 300)
            self.assertEqual(int(trade.added_quantity or 0), 0)
            self.assertEqual(broker.stop_orders[-1]["quantity"], 300)
            self.assertEqual(broker.stop_orders[-1]["trigger_price"], 100.0)
            self.assertEqual(broker.stop_orders[-1]["limit_price"], 99.95)

            broker.orders_by_id["ADD1"] = BrokerOrder("ADD1", "FILLED", 300, 300, 121.0)
            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=3, signals=[make_signal()], option_chain=make_chain(ltp=121),
                states_by_timeframe=make_states(close=121, low=119, high=122, ema9=100), now_market=now,
            )
            self.assertEqual(trade.open_quantity, 300)
            self.assertEqual(int(trade.added_quantity or 0), 0)
            self.assertEqual(broker.stop_orders[-1]["quantity"], 300)
            self.assertEqual(broker.stop_orders[-1]["trigger_price"], 100.0)
            self.assertEqual(broker.stop_orders[-1]["limit_price"], 99.95)

    def test_breakeven_modifies_existing_stop_with_same_quantity(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1)
        broker = ReadinessBroker(lot_size=300)
        now = datetime(2026, 9, 14, 9, 15)
        with Session() as db:
            trade = AutoTrade(
                run_id=1, symbol="HAL", option_symbol="HAL24SEP261500CE", security_id="SEC",
                exchange="NSE", segment="FNO", lot_size=300, tick_size=0.05,
                expiry_date=date(2026, 9, 24), strike=1500, option_type="CE",
                direction="BULLISH", score=85, status="OPEN", dry_run=False,
                management_state="TREND_RIDER", entry_order_id="BUY1", entry_order_status="FILLED",
                initial_quantity=300, filled_quantity=300, open_quantity=300,
                average_entry_price=100, initial_average_price=100, hard_stop_price=90,
                current_ltp=100, sl_order_id="STOP1", sl_order_status="OPEN", sl_quantity=300,
                details={
                    "resolved_instrument": {"buy_allowed": True, "sell_allowed": True},
                    "protective_stop": {
                        "order_id": "STOP1",
                        "quantity": 300,
                        "trigger_price": 90.0,
                        "limit_price": 89.95,
                    },
                },
                created_at=now, updated_at=now,
            )
            db.add(trade)
            broker.orders_by_id["BUY1"] = BrokerOrder("BUY1", "FILLED", 300, 300, 100.0)
            broker.orders_by_id["STOP1"] = BrokerOrder("STOP1", "OPEN", 300, 0, None)

            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(ltp=108),
                states_by_timeframe=make_states(close=108, low=107, high=109, ema9=100), now_market=now,
            )

            self.assertTrue(trade.breakeven_active)
            self.assertEqual(trade.hard_stop_price, 100.0)
            self.assertEqual(trade.sl_quantity, 300)
            self.assertEqual(broker.stop_orders[-1]["order_id"], "STOP1")
            self.assertEqual(broker.stop_orders[-1]["quantity"], 300)
            self.assertEqual(broker.stop_orders[-1]["trigger_price"], 100.0)
            self.assertEqual(broker.stop_orders[-1]["limit_price"], 99.95)
            self.assertEqual(trade.details["protective_stop"]["trigger_price"], 100.0)
            self.assertEqual(trade.details["protective_stop"]["limit_price"], 99.95)

    def test_live_trade_does_not_exit_on_opposite_ema_close(self):
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        settings = Settings(database_url="sqlite://", underlying_symbol="HAL", auto_entry_enabled=True,
                            auto_entry_dry_run=False, auto_entry_stock_min_score=85, auto_entry_lots=1,
                            auto_entry_min_hold_candles=1)
        broker = ReadinessBroker(lot_size=300)
        now = datetime(2026, 9, 14, 9, 15)
        ref = "GEN-1-HAL"
        with Session() as db:
            trade = AutoTrade(
                run_id=1, symbol="HAL", option_symbol="HAL24SEP261500CE", security_id="SEC",
                exchange="NSE", segment="FNO", lot_size=300, tick_size=0.05,
                expiry_date=date(2026, 9, 24), strike=1500, option_type="CE",
                direction="BULLISH", score=85, status="OPEN", dry_run=False,
                management_state="TREND_RIDER", entry_order_id="BUY1", entry_order_status="FILLED",
                initial_quantity=300, filled_quantity=300, open_quantity=300,
                average_entry_price=100, initial_average_price=100, hard_stop_price=90,
                current_ltp=110, sl_order_id="STOP1", sl_order_status="OPEN", sl_quantity=300,
                hold_completed_candles=1, last_action_key=ref,
                details={
                    "resolved_instrument": {"buy_allowed": True, "sell_allowed": True},
                    "protective_stop": {
                        "order_id": "STOP1",
                        "quantity": 300,
                        "trigger_price": 90.0,
                        "limit_price": 89.95,
                    },
                },
                created_at=now, updated_at=now,
            )
            db.add(trade)
            broker.orders_by_id["BUY1"] = BrokerOrder("BUY1", "FILLED", 300, 300, 100.0)
            broker.orders_by_id["STOP1"] = BrokerOrder("STOP1", "OPEN", 300, 0, None)
            AutoTradeService(settings, broker=broker).process(
                db=db, run_id=2, signals=[make_signal()], option_chain=make_chain(ltp=110),
                states_by_timeframe=make_states(close=95, low=94, high=96, ema9=100), now_market=now,
            )
            self.assertEqual(broker.cancelled, [])
            self.assertEqual(broker.stop_orders[-1]["quantity"], 300)
            self.assertEqual(broker.stop_orders[-1]["trigger_price"], 100.0)
            self.assertEqual(broker.stop_orders[-1]["limit_price"], 99.95)
            self.assertEqual(trade.status, "OPEN")
            self.assertTrue(trade.breakeven_active)
            self.assertFalse(trade.trailing_active)
            self.assertIsNone(trade.max_high)
            self.assertEqual(trade.hard_stop_price, 100.0)
            self.assertIsNone(trade.exit_order_id)


if __name__ == "__main__":
    unittest.main()
