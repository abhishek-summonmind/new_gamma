from __future__ import annotations

import logging
from datetime import date
from threading import Lock
from typing import Any, Callable

from ..config import Settings, normalize_market_symbol
from .dhan_service import DhanService

logger = logging.getLogger(__name__)


class S9LTPStreamService:
    """One real Groww LTP subscription for the displayed S9 contracts."""

    def __init__(
        self,
        settings: Settings,
        refresh_service: Any,
        *,
        dhan_service: DhanService | None = None,
        api_factory: Callable[[str], Any] | None = None,
        feed_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._refresh_service = refresh_service
        self._dhan_service = dhan_service or DhanService(settings=settings)
        self._api_factory = api_factory
        self._feed_factory = feed_factory
        self._feed: Any | None = None
        self._instruments: list[dict[str, str]] = []
        self._identity_by_token: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()
        self._lock = Lock()
        self._start_lock = Lock()

    def start(self, callback: Callable[[dict[str, Any]], None]) -> int:
        if str(self._settings.dhan.provider or "").lower() != "groww":
            raise RuntimeError("S9 WebSocket LTP feed currently requires provider=groww")

        instruments, identities = self._resolve_top_instruments()
        if not instruments:
            raise RuntimeError("No resolved S9 Top Opportunity contracts are available for LTP streaming")

        with self._start_lock:
            with self._lock:
                self._listeners.add(callback)
                feed = self._feed

            if feed is None:
                if self._api_factory is None or self._feed_factory is None:
                    from growwapi import GrowwAPI, GrowwFeed

                    api_factory = self._api_factory or GrowwAPI
                    feed_factory = self._feed_factory or GrowwFeed
                else:
                    api_factory = self._api_factory
                    feed_factory = self._feed_factory
                token = self._dhan_service.get_groww_access_token()
                if not token:
                    raise RuntimeError("Groww access token is unavailable for LTP streaming")
                feed = feed_factory(api_factory(token))

            with self._lock:
                self._feed = feed
                old_keys = {self._instrument_key(item) for item in self._instruments}
                new_keys = {self._instrument_key(item) for item in instruments}
                removed = [item for item in self._instruments if self._instrument_key(item) not in new_keys]
                added = [item for item in instruments if self._instrument_key(item) not in old_keys]
                if removed:
                    feed.unsubscribe_ltp(removed)
                self._instruments = instruments
                self._identity_by_token = identities
                if added:
                    feed.subscribe_ltp(added, on_data_received=self._on_data_received)
        logger.info("S9_LTP_STREAM subscribed contracts=%s", len(instruments))
        return len(instruments)

    def remove_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.discard(callback)

    def close(self) -> None:
        with self._lock:
            feed = self._feed
            instruments = self._instruments
            self._feed = None
            self._instruments = []
            self._identity_by_token = {}
            self._listeners.clear()
        if feed is not None and instruments:
            try:
                feed.unsubscribe_ltp(instruments)
            except Exception:  # noqa: BLE001
                logger.debug("S9_LTP_STREAM unsubscribe failed", exc_info=True)


    def _resolve_top_instruments(
        self,
    ) -> tuple[list[dict[str, str]], dict[tuple[str, str, str], dict[str, Any]]]:
        top = self._refresh_service.get_s9_top_opportunities(limit=6)
        rows = top.get("items") if isinstance(top, dict) else None
        instruments: list[dict[str, str]] = []
        identities: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen: set[tuple[str, str, str]] = set()

        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            symbol = normalize_market_symbol(payload.get("underlying_symbol") or item.get("symbol"))
            strike = self._first_value(
                payload.get("selected_strike"), payload.get("final_strike"),
                payload.get("strike"), payload.get("evaluated_strike"),
            )
            option_type = str(self._first_value(
                payload.get("selected_option_type"), payload.get("final_option_type"),
                payload.get("option_type"), payload.get("evaluated_option_type"),
            ) or "").upper()
            strike_scan = payload.get("strike_scan") if isinstance(payload.get("strike_scan"), dict) else {}
            expiry_raw = strike_scan.get("expiry") or payload.get("expiry")
            try:
                expiry = date.fromisoformat(str(expiry_raw))
                strike_value = float(strike)
            except (TypeError, ValueError):
                continue
            if option_type not in {"CE", "PE"}:
                continue
            try:
                contract = self._dhan_service.resolve_groww_option_instrument(
                    underlying=symbol,
                    expiry=expiry,
                    strike=strike_value,
                    option_type=option_type,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "S9_LTP_STREAM instrument unresolved symbol=%s expiry=%s strike=%s type=%s error=%s",
                    symbol, expiry, strike_value, option_type, exc,
                )
                continue
            exchange = str(contract["exchange"]).upper()
            segment = str(contract["segment"]).upper()
            exchange_token = str(contract["security_id"])
            token_key = (exchange, segment, exchange_token)
            if token_key in seen:
                continue
            seen.add(token_key)
            instruments.append({
                "exchange": exchange,
                "segment": segment,
                "exchange_token": exchange_token,
            })
            identities[token_key] = {
                "symbol": symbol,
                "expiry": expiry.isoformat(),
                "strike": strike_value,
                "option_type": option_type,
                "option_symbol": contract.get("trading_symbol"),
                "exchange_token": exchange_token,
            }
        return instruments, identities

    def _on_data_received(self, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            feed = self._feed
            listeners = list(self._listeners)
            identities = dict(self._identity_by_token)
        if feed is None or not listeners:
            return
        meta = meta if isinstance(meta, dict) else {}
        exchange = str(meta.get("exchange") or "").upper()
        segment = str(meta.get("segment") or "").upper()
        exchange_token = str(meta.get("feed_key") or meta.get("exchange_token") or "")
        identity = identities.get((exchange, segment, exchange_token))
        if identity is None:
            return
        try:
            snapshot = feed.get_ltp()
            value = self._nested_ltp(snapshot, exchange, segment, exchange_token)
        except Exception:  # noqa: BLE001
            logger.debug("S9_LTP_STREAM update parse failed meta=%s", meta, exc_info=True)
            return
        if value is None:
            return
        update = {
            "type": "s9_ltp",
            **identity,
            "ltp": value[0],
            "ts_in_millis": value[1],
        }
        for listener in listeners:
            try:
                listener(update)
            except Exception:  # noqa: BLE001
                logger.debug("S9_LTP_STREAM listener failed", exc_info=True)

    @staticmethod
    def _nested_ltp(
        snapshot: Any,
        exchange: str,
        segment: str,
        exchange_token: str,
    ) -> tuple[float, int | float | None] | None:
        root = snapshot.get("ltp") if isinstance(snapshot, dict) and isinstance(snapshot.get("ltp"), dict) else snapshot
        try:
            row = root[exchange][segment][exchange_token]
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
    def _first_value(*values: Any) -> Any:
        return next((value for value in values if value is not None and value != ""), None)

    @staticmethod
    def _instrument_key(instrument: dict[str, str]) -> tuple[str, str, str]:
        return (
            str(instrument.get("exchange") or "").upper(),
            str(instrument.get("segment") or "").upper(),
            str(instrument.get("exchange_token") or ""),
        )
