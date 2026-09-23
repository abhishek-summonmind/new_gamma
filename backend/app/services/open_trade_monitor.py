from __future__ import annotations

import asyncio
import logging
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


class OpenTradeMonitor:
    """Ten-second, quote-only management monitor for OPEN trades."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval_seconds: float = 10.0,
        max_quote_age_seconds: float = 30.0,
        quote_provider: ContractQuoteProvider | None = None,
        trade_service: AutoTradeService | None = None,
        session_factory=SessionLocal,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self._settings = settings
        self._interval_seconds = float(interval_seconds)
        self._max_quote_age_seconds = float(max_quote_age_seconds)
        self._quote_provider = quote_provider or GrowwContractQuoteProvider(settings)
        self._trade_service = trade_service or AutoTradeService(settings)
        self._session_factory = session_factory
        self._market_tz = ZoneInfo(settings.market_timezone)
        self._now_factory = now_factory or (lambda: datetime.now(self._market_tz))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

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
        logger.info("OPEN_TRADE_MONITOR stopped")

    async def _runner(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                await asyncio.to_thread(self.run_cycle)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("OPEN_TRADE_MONITOR cycle_failed")
            remaining = max(0.0, self._interval_seconds - (time.monotonic() - cycle_started))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    def run_cycle(self) -> dict[str, int]:
        contracts = self._open_contracts()
        summary = {"open_trades": len(contracts), "checked": 0, "triggered": 0, "failed": 0, "skipped": 0}
        for contract in contracts:
            checked_at = self._aware_now()
            try:
                quote = self._quote_provider.fetch(contract)
                self._require_fresh_quote(quote, checked_at=checked_at)
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
            if not bool(trade.dry_run):
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
