from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

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
from app.services.open_trade_monitor import (  # noqa: E402
    FreshContractQuote,
    GrowwContractQuoteProvider,
    GrowwOpenTradeLTPStream,
    OpenContractIdentity,
    OpenTradeMonitor,
)
from app.services.auto_trade_service import AutoTradeService  # noqa: E402
from app.services.groww_broker_service import BrokerOrder  # noqa: E402


IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=IST)


class FakeQuoteProvider:
    def __init__(self, quotes: dict[str, FreshContractQuote]):
        self.quotes = quotes
        self.contracts: list[OpenContractIdentity] = []

    def fetch(self, contract: OpenContractIdentity) -> FreshContractQuote:
        self.contracts.append(contract)
        return self.quotes[contract.trading_symbol]


class FakeLiveStream:
    def __init__(self, quotes: dict[str, FreshContractQuote]):
        self.quotes = quotes
        self.contracts: list[OpenContractIdentity] = []
        self.closed = False

    def sync(self, contracts, callback) -> int:
        self.contracts = list(contracts)
        for contract in contracts:
            quote = self.quotes.get(contract.trading_symbol)
            if quote is not None:
                callback(contract, quote)
        return len(contracts)

    def close(self) -> None:
        self.closed = True


class FakeGrowwFeed:
    def __init__(self, _api) -> None:
        self.callback = None
        self.instruments = []
        self.unsubscribed = []
        self.snapshot = {}

    def subscribe_ltp(self, instruments, *, on_data_received) -> None:
        self.instruments.extend(instruments)
        self.callback = on_data_received

    def unsubscribe_ltp(self, instruments) -> None:
        self.unsubscribed.extend(instruments)

    def get_ltp(self) -> dict:
        return self.snapshot


class FakeBroker:
    def __init__(self) -> None:
        self.stop_orders: list[dict] = []
        self.orders_by_id: dict[str, BrokerOrder] = {}

    def place_stop_order(self, **kwargs):
        self.stop_orders.append(kwargs)
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def modify_stop(self, **kwargs):
        self.stop_orders.append(kwargs)
        return BrokerOrder("STOP1", "OPEN", kwargs["quantity"], 0, None)

    def cancel_order(self, order_id):
        return BrokerOrder(order_id, "CANCELLED", 0, 0, None)

    def get_order(self, order_id):
        return self.orders_by_id.get(order_id, BrokerOrder(order_id, "OPEN", 1, 0, None))


class TestOpenTradeMonitor(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.settings = Settings(database_url="sqlite://", market_timezone="Asia/Kolkata")

    def add_trade(
        self,
        *,
        symbol: str,
        option_symbol: str,
        ltp: float = 120.0,
        stop: float = 100.0,
        status: str = "OPEN",
        dry_run: bool = True,
        strike: float = 1500.0,
        option_type: str = "CE",
        average_entry_price: float = 110.0,
        hard_stop_price: float | None = None,
        sl_order_id: str | None = None,
    ) -> int:
        stop_price = stop if hard_stop_price is None else hard_stop_price
        with self.Session() as db:
            trade = AutoTrade(
                symbol=symbol,
                option_symbol=option_symbol,
                security_id=f"TOKEN-{option_symbol}",
                exchange="NSE",
                segment="FNO",
                expiry_date=date(2026, 9, 24),
                strike=strike,
                option_type=option_type,
                direction="BULLISH",
                score=90.0,
                status=status,
                dry_run=dry_run,
                management_state=status,
                initial_quantity=1,
                filled_quantity=1,
                open_quantity=1 if status == "OPEN" else 0,
                average_entry_price=average_entry_price,
                initial_average_price=average_entry_price,
                hard_stop_price=stop_price,
                current_ltp=ltp,
                sl_order_id=sl_order_id,
                sl_order_status="OPEN" if sl_order_id else ("DRY_RUN_ACTIVE" if dry_run else None),
                sl_quantity=1 if sl_order_id else 0,
                details={
                    "resolved_instrument": {
                        "buy_allowed": True,
                        "sell_allowed": True,
                    }
                },
            )
            db.add(trade)
            db.commit()
            return int(trade.id)

    def monitor(self, provider: FakeQuoteProvider, **kwargs) -> OpenTradeMonitor:
        return OpenTradeMonitor(
            self.settings,
            quote_provider=provider,
            session_factory=self.Session,
            now_factory=lambda: NOW,
            **kwargs,
        )

    @staticmethod
    def fresh(ltp: float) -> FreshContractQuote:
        return FreshContractQuote(ltp=ltp, observed_at=NOW - timedelta(seconds=1))

    def get_trade(self, trade_id: int) -> AutoTrade:
        with self.Session() as db:
            return db.get(AutoTrade, trade_id)

    def test_ltp_above_stop_updates_ltp_and_remains_open(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            stop=213.55,
            average_entry_price=237.3,
        )
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": self.fresh(223.65)})

        summary = self.monitor(provider).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary, {"open_trades": 1, "checked": 1, "triggered": 0, "failed": 0, "skipped": 0})
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.current_ltp, 223.65)
        self.assertIsNone(trade.exit_price)

    def test_ltp_equal_stop_closes(self) -> None:
        trade_id = self.add_trade(symbol="BSE", option_symbol="BSE26SEP3200CE", stop=78.0)
        provider = FakeQuoteProvider({"BSE26SEP3200CE": self.fresh(78.0)})

        self.monitor(provider).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.exit_price, 78.0)
        self.assertEqual(trade.exit_reason, "HARD_STOP_LOSS")

    def test_ltp_below_stop_closes_at_observed_ltp(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            stop=213.55,
            average_entry_price=237.3,
        )
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": self.fresh(201.95)})

        summary = self.monitor(provider).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary["triggered"], 1)
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.current_ltp, 201.95)
        self.assertEqual(trade.exit_price, 201.95)
        self.assertEqual(trade.exit_reason, "HARD_STOP_LOSS")

    def test_multiple_open_trades_checked_and_closed_trades_ignored(self) -> None:
        first = self.add_trade(symbol="RELIANCE", option_symbol="RELIANCE26SEP1240CE", stop=16.9)
        second = self.add_trade(symbol="TCS", option_symbol="TCS26SEP2100CE", stop=33.9)
        closed = self.add_trade(symbol="BSE", option_symbol="BSE26SEP3200CE", stop=78.0, status="CLOSED")
        provider = FakeQuoteProvider(
            {
                "RELIANCE26SEP1240CE": self.fresh(17.1),
                "TCS26SEP2100CE": self.fresh(32.95),
            }
        )

        summary = self.monitor(provider).run_cycle()

        self.assertEqual(summary["open_trades"], 2)
        self.assertEqual([row.trading_symbol for row in provider.contracts], ["RELIANCE26SEP1240CE", "TCS26SEP2100CE"])
        self.assertEqual(self.get_trade(first).status, "OPEN")
        self.assertEqual(self.get_trade(second).status, "CLOSED")
        self.assertEqual(self.get_trade(closed).status, "CLOSED")

    def test_exact_stored_contract_identity_is_sent_to_provider(self) -> None:
        self.add_trade(
            symbol="TCS",
            option_symbol="TCS26SEP2100PE",
            stop=33.9,
            strike=2100.0,
            option_type="PE",
        )
        provider = FakeQuoteProvider({"TCS26SEP2100PE": self.fresh(40.0)})

        self.monitor(provider).run_cycle()
        identity = provider.contracts[0]

        self.assertEqual(identity.security_id, "TOKEN-TCS26SEP2100PE")
        self.assertEqual(identity.trading_symbol, "TCS26SEP2100PE")
        self.assertEqual(identity.strike, 2100.0)
        self.assertEqual(identity.expiry, date(2026, 9, 24))
        self.assertEqual(identity.option_type, "PE")
        self.assertEqual(identity.underlying, "TCS")
        self.assertEqual(identity.exchange, "NSE")
        self.assertEqual(identity.segment, "FNO")

    def test_stale_quote_cannot_update_or_trigger_exit(self) -> None:
        trade_id = self.add_trade(symbol="RELIANCE", option_symbol="RELIANCE26SEP1240CE", ltp=17.35, stop=16.9)
        stale = FreshContractQuote(ltp=10.0, observed_at=NOW - timedelta(minutes=2))
        provider = FakeQuoteProvider({"RELIANCE26SEP1240CE": stale})

        summary = self.monitor(provider, max_quote_age_seconds=30).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.current_ltp, 17.35)
        self.assertIsNone(trade.exit_price)

    def test_duplicate_close_is_prevented_by_locked_open_state_recheck(self) -> None:
        trade_id = self.add_trade(symbol="TCS", option_symbol="TCS26SEP2100CE", stop=33.9)
        provider = FakeQuoteProvider({"TCS26SEP2100CE": self.fresh(32.95)})
        monitor = self.monitor(provider)
        identity = monitor._open_contracts()[0]  # noqa: SLF001
        quote = provider.fetch(identity)

        first = monitor._apply_quote(identity, quote=quote, checked_at=NOW)  # noqa: SLF001
        first_exit_order_id = self.get_trade(trade_id).exit_order_id
        second = monitor._apply_quote(identity, quote=quote, checked_at=NOW)  # noqa: SLF001
        trade = self.get_trade(trade_id)

        self.assertEqual(first, "triggered")
        self.assertEqual(second, "skipped")
        self.assertEqual(trade.exit_order_id, first_exit_order_id)
        self.assertEqual(trade.exit_filled_quantity, 1)

    def test_monitor_does_not_invoke_full_scanner(self) -> None:
        self.add_trade(symbol="BSE", option_symbol="BSE26SEP3200CE", stop=78.0)
        provider = FakeQuoteProvider({"BSE26SEP3200CE": self.fresh(80.0)})
        with patch("app.services.refresh_service.RefreshService.run_refresh") as full_scan:
            self.monitor(provider).run_cycle()
        full_scan.assert_not_called()

    def test_live_ltp_shifts_stop_to_breakeven_before_exit_check(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            average_entry_price=100.0,
            hard_stop_price=90.0,
        )
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": self.fresh(108.0)})

        self.monitor(provider).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(trade.status, "OPEN")
        self.assertTrue(trade.breakeven_active)
        self.assertEqual(trade.hard_stop_price, 100.0)
        self.assertEqual(trade.sl_order_status, "DRY_RUN_BREAKEVEN")

    def test_live_ltp_applies_trailing_exit_at_observed_ltp(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            average_entry_price=100.0,
            hard_stop_price=90.0,
        )
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": self.fresh(115.0)})
        monitor = self.monitor(provider)
        monitor.run_cycle()
        provider.quotes["SENSEX26SEP74700CE"] = self.fresh(112.0)

        summary = monitor.run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary["triggered"], 1)
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.exit_price, 112.0)
        self.assertEqual(trade.exit_reason, "TRAILING_MAX_HIGH_MINUS_3")

    def test_stale_quote_cannot_shift_stop(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            average_entry_price=100.0,
            hard_stop_price=90.0,
        )
        stale = FreshContractQuote(ltp=108.0, observed_at=NOW - timedelta(minutes=2))
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": stale})

        summary = self.monitor(provider, max_quote_age_seconds=30).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary["failed"], 1)
        self.assertFalse(trade.breakeven_active)
        self.assertEqual(trade.hard_stop_price, 90.0)

    def test_live_trade_uses_existing_broker_stop_safety_on_shift(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            average_entry_price=100.0,
            hard_stop_price=90.0,
            dry_run=False,
            sl_order_id="STOP1",
        )
        broker = FakeBroker()
        provider = FakeQuoteProvider({"SENSEX26SEP74700CE": self.fresh(108.0)})
        service = AutoTradeService(self.settings, broker=broker)

        summary = self.monitor(provider, trade_service=service).run_cycle()
        trade = self.get_trade(trade_id)

        self.assertEqual(summary["checked"], 1)
        self.assertEqual(trade.status, "OPEN")
        self.assertTrue(trade.breakeven_active)
        self.assertEqual(trade.hard_stop_price, 100.0)
        self.assertEqual(broker.stop_orders[-1]["trigger_price"], 100.0)

    def test_groww_provider_uses_exact_contract_quote_endpoint(self) -> None:
        service = Mock()
        service.get_json.return_value = {
            "payload": {
                "last_price": 201.95,
                "last_trade_time": int(NOW.timestamp() * 1000),
            }
        }
        identity = OpenContractIdentity(
            trade_id=1,
            security_id="EXCHANGE-TOKEN",
            trading_symbol="SENSEX26SEP74700CE",
            strike=74700.0,
            expiry=date(2026, 9, 24),
            option_type="CE",
            underlying="SENSEX",
            exchange="BSE",
            segment="FNO",
            hard_stop_price=213.55,
        )

        quote = GrowwContractQuoteProvider(self.settings, service=service).fetch(identity)

        self.assertEqual(quote.ltp, 201.95)
        service.get_json.assert_called_once_with(
            "/live-data/quote",
            params={"exchange": "BSE", "segment": "FNO", "trading_symbol": "SENSEX26SEP74700CE"},
        )

    def test_live_stream_tick_drives_trailing_exit_without_rest_poll(self) -> None:
        trade_id = self.add_trade(
            symbol="SENSEX",
            option_symbol="SENSEX26SEP74700CE",
            average_entry_price=100.0,
            hard_stop_price=90.0,
        )
        stream = FakeLiveStream({"SENSEX26SEP74700CE": self.fresh(115.0)})
        provider = Mock()
        monitor = self.monitor(provider, live_stream=stream)

        monitor._run_live_cycle()  # noqa: SLF001
        stream.quotes["SENSEX26SEP74700CE"] = self.fresh(112.0)
        monitor._run_live_cycle()  # noqa: SLF001
        trade = self.get_trade(trade_id)

        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.exit_reason, "TRAILING_MAX_HIGH_MINUS_3")
        provider.fetch.assert_not_called()

    def test_exit_pending_live_order_is_reconciled_after_subscription_is_removed(self) -> None:
        trade_id = self.add_trade(
            symbol="TCS",
            option_symbol="TCS26SEP2100CE",
            dry_run=False,
        )
        with self.Session() as db:
            trade = db.get(AutoTrade, trade_id)
            trade.status = "EXIT_PENDING"
            trade.management_state = "EXIT_PENDING"
            trade.exit_order_id = "EXIT1"
            trade.exit_order_status = "OPEN"
            trade.exit_requested_quantity = 1
            trade.exit_reason = "TRAILING_MAX_HIGH_MINUS_3"
            db.commit()
        broker = FakeBroker()
        broker.orders_by_id["EXIT1"] = BrokerOrder("EXIT1", "FILLED", 1, 1, 125.0)
        service = AutoTradeService(self.settings, broker=broker)
        monitor = self.monitor(Mock(), live_stream=FakeLiveStream({}), trade_service=service)

        monitor._run_live_cycle()  # noqa: SLF001
        trade = self.get_trade(trade_id)

        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.open_quantity, 0)
        self.assertEqual(trade.exit_price, 125.0)

    def test_groww_live_stream_subscribes_exact_open_contract_and_emits_tick(self) -> None:
        feed_holder = {}

        def feed_factory(api):
            feed_holder["feed"] = FakeGrowwFeed(api)
            return feed_holder["feed"]

        service = Mock()
        service.get_groww_access_token.return_value = "token"
        stream = GrowwOpenTradeLTPStream(
            self.settings,
            service=service,
            api_factory=lambda token: {"token": token},
            feed_factory=feed_factory,
        )
        contract = OpenContractIdentity(
            trade_id=7,
            security_id="98765",
            trading_symbol="SENSEX26SEP74700CE",
            strike=74700.0,
            expiry=date(2026, 9, 24),
            option_type="CE",
            underlying="SENSEX",
            exchange="BSE",
            segment="FNO",
            hard_stop_price=200.0,
        )
        updates = []

        self.assertEqual(stream.sync([contract], lambda identity, quote: updates.append((identity, quote))), 1)
        feed = feed_holder["feed"]
        feed.snapshot = {
            "BSE": {"FNO": {"98765": {"ltp": 223.65, "tsInMillis": int(NOW.timestamp() * 1000)}}}
        }
        feed.callback({"exchange": "BSE", "segment": "FNO", "feed_key": "98765"})

        self.assertEqual(feed.instruments, [{"exchange": "BSE", "segment": "FNO", "exchange_token": "98765"}])
        self.assertEqual(updates[0][0], contract)
        self.assertEqual(updates[0][1].ltp, 223.65)
        stream.close()
        self.assertEqual(feed.unsubscribed, feed.instruments)


class TestOpenTradeMonitorSchedule(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_defaults_to_ten_seconds_and_runs_on_its_own_loop(self) -> None:
        settings = Settings(database_url="sqlite://")
        monitor = OpenTradeMonitor(settings, quote_provider=Mock())
        self.assertEqual(monitor._interval_seconds, 10.0)  # noqa: SLF001

        fast_monitor = OpenTradeMonitor(settings, interval_seconds=0.02, quote_provider=Mock())
        cycles = 0

        def cycle() -> dict[str, int]:
            nonlocal cycles
            cycles += 1
            return {"open_trades": 0, "checked": 0, "triggered": 0, "failed": 0, "skipped": 0}

        fast_monitor.run_cycle = cycle  # type: ignore[method-assign]
        await fast_monitor.start()
        await asyncio.sleep(0.075)
        await fast_monitor.stop()

        self.assertGreaterEqual(cycles, 3)


if __name__ == "__main__":
    unittest.main()
