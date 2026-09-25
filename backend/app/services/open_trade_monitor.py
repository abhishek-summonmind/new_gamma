from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import Settings
from ..db import SessionLocal
from ..models import AutoTrade
from .auto_trade_service import AutoTradeService
from .dhan_service import DhanService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenContractIdentity:
    trade_id: int
    security_id: str
    trading_symbol: str
    strike: float
    expiry: date
    option_type: str
    underlying: str
    exchange: str
    segment: str
    hard_stop_price: float


@dataclass(frozen=True)
class FreshContractQuote:
    ltp: float
    observed_at: datetime


class ContractQuoteProvider(Protocol):
    def fetch(self, contract: OpenContractIdentity) -> FreshContractQuote: ...


class ContractLTPStream(Protocol):
    def sync(
        self,
        contracts: list[OpenContractIdentity],
        callback: Callable[[OpenContractIdentity, FreshContractQuote], None],
    ) -> int: ...

    def close(self) -> None: ...


class GrowwContractQuoteProvider:
    """Fetch an uncached quote for one exact exchange contract."""

    def __init__(self, settings: Settings, *, service: DhanService | None = None):
        self._service = service or DhanService(settings=settings)

    def fetch(self, contract: OpenContractIdentity) -> FreshContractQuote:
        data = self._service.get_json(
            "/live-data/quote",
            params={
                "exchange": contract.exchange,
                "segment": contract.segment,
                "trading_symbol": contract.trading_symbol,
            },
        )
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise RuntimeError("Groww quote response has no payload")
        try:
            ltp = float(payload.get("last_price"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Groww quote response has no valid last_price") from exc
        if ltp <= 0:
            raise RuntimeError("Groww quote response has a non-positive last_price")
        observed_at = self._parse_last_trade_time(payload.get("last_trade_time"))
        if observed_at is None:
            raise RuntimeError("Groww quote response has no valid last_trade_time")
        return FreshContractQuote(ltp=ltp, observed_at=observed_at)

    @staticmethod
    def _parse_last_trade_time(value: object) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        # Groww documents last_trade_time as epoch milliseconds. Accept epoch
        # seconds as well so a provider-side representation change fails safe.
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None


class GrowwOpenTradeLTPStream:
    """Maintain exact Groww LTP subscriptions for all currently OPEN contracts."""

    def __init__(
        self,
        settings: Settings,
        *,
        service: DhanService | None = None,
        api_factory: Callable[[str], Any] | None = None,
        feed_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._service = service or DhanService(settings=settings)
        self._api_factory = api_factory
        self._feed_factory = feed_factory
        self._feed: Any | None = None
        self._instruments: list[dict[str, str]] = []
        self._contracts_by_token: dict[tuple[str, str, str], list[OpenContractIdentity]] = {}
        self._callback: Callable[[OpenContractIdentity, FreshContractQuote], None] | None = None
        self._lock = threading.RLock()

    def sync(
        self,
        contracts: list[OpenContractIdentity],
        callback: Callable[[OpenContractIdentity, FreshContractQuote], None],
    ) -> int:
        instruments, contracts_by_token = self._subscription_data(contracts)
        with self._lock:
            self._callback = callback
            feed = self._feed
            if feed is None and instruments:
                if self._api_factory is None or self._feed_factory is None:
                    from growwapi import GrowwAPI, GrowwFeed

                    api_factory = self._api_factory or GrowwAPI
                    feed_factory = self._feed_factory or GrowwFeed
                else:
                    api_factory = self._api_factory
                    feed_factory = self._feed_factory
                token = self._service.get_groww_access_token()
                if not token:
                    raise RuntimeError("Groww access token is unavailable for open-trade LTP streaming")
                feed = feed_factory(api_factory(token))
                self._feed = feed

            old_keys = {self._instrument_key(item) for item in self._instruments}
            new_keys = {self._instrument_key(item) for item in instruments}
            removed = [item for item in self._instruments if self._instrument_key(item) not in new_keys]
            added = [item for item in instruments if self._instrument_key(item) not in old_keys]
            if feed is not None and removed:
                feed.unsubscribe_ltp(removed)
            self._instruments = instruments
            self._contracts_by_token = contracts_by_token
            if feed is not None and added:
                feed.subscribe_ltp(added, on_data_received=self._on_data_received)
        return len(instruments)

    def close(self) -> None:
        with self._lock:
            feed = self._feed
            instruments = self._instruments
            self._feed = None
            self._instruments = []
            self._contracts_by_token = {}
            self._callback = None
        if feed is not None and instruments:
            try:
                feed.unsubscribe_ltp(instruments)
            except Exception:  # noqa: BLE001
                logger.debug("OPEN_TRADE_STREAM unsubscribe_failed", exc_info=True)

    def _on_data_received(self, meta: dict[str, Any] | None = None) -> None:
        meta = meta if isinstance(meta, dict) else {}
        key = (
            str(meta.get("exchange") or "").upper(),
            str(meta.get("segment") or "").upper(),
            str(meta.get("feed_key") or meta.get("exchange_token") or ""),
        )
        with self._lock:
            feed = self._feed
            contracts = list(self._contracts_by_token.get(key, []))
            callback = self._callback
        if feed is None or callback is None or not contracts:
            return
        try:
            snapshot = feed.get_ltp()
            value = self._nested_ltp(snapshot, *key)
            if value is None:
                return
            observed_at = GrowwContractQuoteProvider._parse_last_trade_time(value[1])
            if observed_at is None:
                raise RuntimeError("Groww stream tick has no valid timestamp")
            quote = FreshContractQuote(ltp=value[0], observed_at=observed_at)
        except Exception:  # noqa: BLE001
            logger.warning("OPEN_TRADE_STREAM invalid_tick token=%s", key, exc_info=True)
            return
        for contract in contracts:
            try:
                callback(contract, quote)
            except Exception:  # noqa: BLE001
                logger.exception("OPEN_TRADE_STREAM callback_failed trade_id=%s", contract.trade_id)

    @staticmethod
    def _subscription_data(
        contracts: list[OpenContractIdentity],
    ) -> tuple[list[dict[str, str]], dict[tuple[str, str, str], list[OpenContractIdentity]]]:
        instruments: list[dict[str, str]] = []
        contracts_by_token: dict[tuple[str, str, str], list[OpenContractIdentity]] = {}
        for contract in contracts:
            key = (contract.exchange.upper(), contract.segment.upper(), contract.security_id)
            if key not in contracts_by_token:
                instruments.append({"exchange": key[0], "segment": key[1], "exchange_token": key[2]})
                contracts_by_token[key] = []
            contracts_by_token[key].append(contract)
        return instruments, contracts_by_token

    @staticmethod
    def _nested_ltp(snapshot: Any, exchange: str, segment: str, token: str) -> tuple[float, object] | None:
        root = snapshot.get("ltp") if isinstance(snapshot, dict) and isinstance(snapshot.get("ltp"), dict) else snapshot
        try:
            row = root[exchange][segment][token]
        except (KeyError, TypeError):
            return None
        if isinstance(row, dict):
            raw_ltp = row.get("ltp")
            timestamp = row.get("tsInMillis") or row.get("ts_in_millis")
        else:
            raw_ltp, timestamp = row, None
        try:
            ltp = float(raw_ltp)
        except (TypeError, ValueError):
            return None
        return (ltp, timestamp) if ltp > 0 else None

    @staticmethod
    def _instrument_key(instrument: dict[str, str]) -> tuple[str, str, str]:
        return (
            str(instrument.get("exchange") or "").upper(),
            str(instrument.get("segment") or "").upper(),
            str(instrument.get("exchange_token") or ""),
        )


class OpenTradeMonitor:
    """Manage OPEN trades from live ticks, with stale-stream quote fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval_seconds: float = 10.0,
        max_quote_age_seconds: float = 30.0,
        quote_provider: ContractQuoteProvider | None = None,
        live_stream: ContractLTPStream | None = None,
        trade_service: AutoTradeService | None = None,
        session_factory=SessionLocal,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self._settings = settings
        self._interval_seconds = float(interval_seconds)
        self._max_quote_age_seconds = float(max_quote_age_seconds)
        custom_quote_provider = quote_provider is not None
        self._quote_provider = quote_provider or GrowwContractQuoteProvider(settings)
        self._live_stream = live_stream
        if live_stream is None and not custom_quote_provider and str(settings.dhan.provider or "").lower() == "groww":
            self._live_stream = GrowwOpenTradeLTPStream(settings)
        self._trade_service = trade_service or AutoTradeService(settings)
        self._session_factory = session_factory
        self._market_tz = ZoneInfo(settings.market_timezone)
        self._now_factory = now_factory or (lambda: datetime.now(self._market_tz))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._management_lock = threading.RLock()
        self._last_live_tick: dict[int, float] = {}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._runner(), name="open-trade-monitor")
        logger.info("OPEN_TRADE_MONITOR started interval_seconds=%.1f open_trades_only=true", self._interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        if self._live_stream is not None:
            await asyncio.to_thread(self._live_stream.close)
        logger.info("OPEN_TRADE_MONITOR stopped")

    async def _runner(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                cycle = self._run_live_cycle if self._live_stream is not None else self.run_cycle
                await asyncio.to_thread(cycle)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("OPEN_TRADE_MONITOR cycle_failed")
            remaining = max(0.0, self._interval_seconds - (time.monotonic() - cycle_started))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    def _run_live_cycle(self) -> dict[str, int]:
        contracts = self._open_contracts()
        assert self._live_stream is not None
        try:
            subscribed = self._live_stream.sync(contracts, self._on_live_quote)
        except Exception:  # noqa: BLE001
            logger.exception("OPEN_TRADE_STREAM subscription_sync_failed; using quote fallback")
            self._reconcile_live_orders()
            return self._check_contracts(contracts)
        self._reconcile_live_orders()

        now_monotonic = time.monotonic()
        active_ids = {contract.trade_id for contract in contracts}
        with self._management_lock:
            self._last_live_tick = {
                trade_id: observed for trade_id, observed in self._last_live_tick.items() if trade_id in active_ids
            }
            stale_contracts = [
                contract
                for contract in contracts
                if now_monotonic - self._last_live_tick.get(contract.trade_id, float("-inf"))
                > self._max_quote_age_seconds
            ]
        summary = self._check_contracts(stale_contracts)
        summary["open_trades"] = len(contracts)
        logger.debug(
            "OPEN_TRADE_MONITOR live_cycle subscriptions=%s fallback_quotes=%s",
            subscribed,
            len(stale_contracts),
        )
        return summary

    def _on_live_quote(self, contract: OpenContractIdentity, quote: FreshContractQuote) -> None:
        checked_at = self._aware_now()
        try:
            self._require_fresh_quote(quote, checked_at=checked_at)
            with self._management_lock:
                result = self._apply_quote(
                    contract,
                    quote=quote,
                    checked_at=checked_at,
                    reconcile_live=False,
                )
                if result != "skipped":
                    self._last_live_tick[contract.trade_id] = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OPEN_TRADE_MONITOR live_tick_failed contract=%s LTP=%.4f checked_at=%s error=%s",
                self._contract_log_value(contract),
                quote.ltp,
                checked_at.isoformat(),
                exc,
            )

    def _reconcile_live_orders(self) -> None:
        now_market = self._aware_now()
        with self._management_lock, self._session_factory() as db:
            trades = list(
                db.scalars(
                    select(AutoTrade)
                    .where(
                        AutoTrade.status.in_(("OPEN", "EXIT_PENDING")),
                        AutoTrade.dry_run.is_(False),
                    )
                    .order_by(AutoTrade.id.asc())
                    .with_for_update()
                )
            )
            for trade in trades:
                summary: dict[str, Any] = {"exits": 0, "adds": 0, "events": [], "entry_rejections": []}
                try:
                    self._trade_service._reconcile_live_trade(  # noqa: SLF001
                        trade=trade,
                        now_market=now_market,
                        summary=summary,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("OPEN_TRADE_MONITOR broker_reconcile_failed trade_id=%s", trade.id)
            db.commit()

    def run_cycle(self) -> dict[str, int]:
        contracts = self._open_contracts()
        return self._check_contracts(contracts)

    def _check_contracts(self, contracts: list[OpenContractIdentity]) -> dict[str, int]:
        summary = {"open_trades": len(contracts), "checked": 0, "triggered": 0, "failed": 0, "skipped": 0}
        for contract in contracts:
            checked_at = self._aware_now()
            try:
                quote = self._quote_provider.fetch(contract)
                self._require_fresh_quote(quote, checked_at=checked_at)
                with self._management_lock:
                    result = self._apply_quote(contract, quote=quote, checked_at=checked_at)
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                logger.warning(
                    "OPEN_TRADE_MONITOR contract=%s LTP=unavailable hard_stop=%.4f checked_at=%s "
                    "triggered=false error=%s",
                    self._contract_log_value(contract),
                    contract.hard_stop_price,
                    checked_at.isoformat(),
                    exc,
                )
                continue
            if result == "skipped":
                summary["skipped"] += 1
                continue
            summary["checked"] += 1
            if result == "triggered":
                summary["triggered"] += 1
        return summary

    def _open_contracts(self) -> list[OpenContractIdentity]:
        with self._session_factory() as db:
            trades = list(
                db.scalars(
                    select(AutoTrade)
                    .where(AutoTrade.status == "OPEN")
                    .order_by(AutoTrade.id.asc())
                )
            )
        contracts: list[OpenContractIdentity] = []
        for trade in trades:
            missing = [
                name
                for name, value in (
                    ("security_id", trade.security_id),
                    ("trading_symbol", trade.option_symbol),
                    ("expiry", trade.expiry_date),
                    ("option_type", trade.option_type),
                    ("underlying", trade.symbol),
                    ("exchange", trade.exchange),
                )
                if value in {None, ""}
            ]
            if missing:
                logger.warning(
                    "OPEN_TRADE_MONITOR trade_id=%s contract_identity_invalid missing=%s",
                    trade.id,
                    ",".join(missing),
                )
                continue
            contracts.append(
                OpenContractIdentity(
                    trade_id=int(trade.id),
                    security_id=str(trade.security_id),
                    trading_symbol=str(trade.option_symbol),
                    strike=float(trade.strike),
                    expiry=trade.expiry_date,
                    option_type=str(trade.option_type).upper(),
                    underlying=str(trade.symbol),
                    exchange=str(trade.exchange).upper(),
                    segment=str(trade.segment or "FNO").upper(),
                    hard_stop_price=float(trade.hard_stop_price or 0.0),
                )
            )
        return contracts

    def _apply_quote(
        self,
        contract: OpenContractIdentity,
        *,
        quote: FreshContractQuote,
        checked_at: datetime,
        reconcile_live: bool = True,
    ) -> str:
        with self._session_factory() as db:
            trade = db.scalar(
                select(AutoTrade)
                .where(
                    AutoTrade.id == contract.trade_id,
                    AutoTrade.status == "OPEN",
                )
                .with_for_update()
            )
            if trade is None:
                db.rollback()
                logger.info(
                    "OPEN_TRADE_MONITOR contract=%s LTP=%.4f hard_stop=unavailable checked_at=%s "
                    "triggered=false skipped=not_open",
                    self._contract_log_value(contract),
                    quote.ltp,
                    checked_at.isoformat(),
                )
                return "skipped"
            if not self._identity_matches(trade, contract):
                db.rollback()
                raise RuntimeError("stored contract identity changed before quote application")

            ltp = float(quote.ltp)
            management_summary: dict[str, Any] = {
                "exits": 0,
                "adds": 0,
                "events": [],
                "entry_rejections": [],
            }
            if not bool(trade.dry_run) and reconcile_live:
                self._trade_service._reconcile_live_trade(  # noqa: SLF001
                    trade=trade,
                    now_market=checked_at,
                    summary=management_summary,
                )
                if trade.status != "OPEN":
                    db.commit()
                    logger.info(
                        "OPEN_TRADE_MONITOR contract=%s LTP=%.4f hard_stop=unavailable checked_at=%s "
                        "triggered=false skipped=live_reconciled_closed",
                        self._contract_log_value(contract),
                        ltp,
                        checked_at.isoformat(),
                    )
                    return "skipped"

            hard_stop_before = float(trade.hard_stop_price or 0.0)
            if hard_stop_before <= 0:
                db.rollback()
                raise RuntimeError("OPEN trade has no valid hard_stop_price")

            previous_status = str(trade.status or "")
            exit_reason = self._trade_service.manage_open_trade_ltp(
                trade=trade,
                ltp=ltp,
                now_market=checked_at,
                summary=management_summary,
            )
            hard_stop = float(trade.hard_stop_price or 0.0)
            effective_stop = self._effective_stop_price(trade, hard_stop=hard_stop)
            triggered = exit_reason is not None or (previous_status == "OPEN" and trade.status != "OPEN")
            logger.info(
                "OPEN_TRADE_MONITOR contract=%s LTP=%.4f hard_stop=%.4f effective_stop=%.4f "
                "checked_at=%s stop_shifted=%s triggered=%s",
                self._contract_log_value(contract),
                ltp,
                hard_stop,
                effective_stop,
                checked_at.isoformat(),
                str(hard_stop != hard_stop_before).lower(),
                str(triggered).lower(),
            )
            if triggered:
                logger.warning(
                    "SL_TRIGGER trade_id=%s hard_stop=%.4f observed_ltp=%.4f exit_price=%.4f "
                    "exit_reason=%s triggered_at=%s",
                    trade.id,
                    hard_stop,
                    ltp,
                    float(trade.exit_price if trade.exit_price is not None else ltp),
                    trade.exit_reason or exit_reason or "UNKNOWN",
                    checked_at.isoformat(),
                )
            db.commit()
            return "triggered" if triggered else "checked"

    def _require_fresh_quote(self, quote: FreshContractQuote, *, checked_at: datetime) -> None:
        observed = quote.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        checked_utc = checked_at.astimezone(timezone.utc)
        age_seconds = (checked_utc - observed).total_seconds()
        if age_seconds < -5.0:
            raise RuntimeError(f"quote timestamp is in the future by {-age_seconds:.1f}s")
        if age_seconds > self._max_quote_age_seconds:
            raise RuntimeError(
                f"stale quote age_seconds={age_seconds:.1f} max_age_seconds={self._max_quote_age_seconds:.1f}"
            )

    def _aware_now(self) -> datetime:
        value = self._now_factory()
        return value.replace(tzinfo=self._market_tz) if value.tzinfo is None else value.astimezone(self._market_tz)

    @staticmethod
    def _identity_matches(trade: AutoTrade, contract: OpenContractIdentity) -> bool:
        return (
            str(trade.security_id or "") == contract.security_id
            and str(trade.option_symbol or "") == contract.trading_symbol
            and float(trade.strike) == contract.strike
            and trade.expiry_date == contract.expiry
            and str(trade.option_type or "").upper() == contract.option_type
            and str(trade.symbol or "") == contract.underlying
            and str(trade.exchange or "").upper() == contract.exchange
            and str(trade.segment or "FNO").upper() == contract.segment
        )

    @staticmethod
    def _effective_stop_price(trade: AutoTrade, *, hard_stop: float) -> float:
        effective_stop = float(hard_stop)
        if bool(trade.trailing_active) and trade.max_high is not None:
            effective_stop = max(effective_stop, float(trade.max_high) - 3.0)
        return effective_stop

    @staticmethod
    def _contract_log_value(contract: OpenContractIdentity) -> str:
        return (
            f"trade_id={contract.trade_id},security_id={contract.security_id},"
            f"trading_symbol={contract.trading_symbol},strike={contract.strike:g},"
            f"expiry={contract.expiry.isoformat()},option_type={contract.option_type},"
            f"underlying={contract.underlying},exchange={contract.exchange},segment={contract.segment}"
        )
