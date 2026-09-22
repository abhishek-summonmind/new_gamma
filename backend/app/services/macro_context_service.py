from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import requests

from ..config import Settings

logger = logging.getLogger(__name__)


class MacroContextService:
    """Fetch and cache the daily macro comparison inputs used by S9."""

    _lock = Lock()
    _cached_at: datetime | None = None
    _cached_value: dict[str, Any] | None = None
    _YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    _NSE_FLOWS = "https://www.nseindia.com/api/fiidiiTradeReact"

    def __init__(self, settings: Settings, *, session: requests.Session | None = None):
        self._settings = settings
        self._session = session or requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; GammaS9/1.0)",
            "Accept": "application/json,text/plain,*/*",
        })

    def get_context(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        ttl = timedelta(seconds=max(60, int(self._settings.public_macro_cache_seconds)))
        with self._lock:
            if self._cached_at and self._cached_value and current - self._cached_at < ttl:
                return dict(self._cached_value)

        jobs = {
            "india_vix": self._india_vix,
            "brent": lambda: self._quote("BZ%3DF"),
            "usd_inr": lambda: self._quote("INR%3DX"),
            "fii_dii": self._fii_dii,
        }
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="macro") as pool:
            futures = {name: pool.submit(loader) for name, loader in jobs.items()}
            for name, future in futures.items():
                try:
                    values[name] = future.result()
                except Exception as exc:  # noqa: BLE001
                    errors[name] = str(exc)
                    logger.warning("Macro source unavailable source=%s error=%s", name, exc)
        context = {
            **values,
            "as_of": current.isoformat(),
            "errors": errors,
            "confirmation_only": True,
        }
        with self._lock:
            self.__class__._cached_at = current
            self.__class__._cached_value = dict(context)
        return context

    def _india_vix(self) -> dict[str, Any]:
        quote = self._quote("%5EINDIAVIX")
        return {
            **quote,
            "comparison_symbol": "INDIA_VIX",
            "timeframe": "1D",
            "indicator": "price_change_percentage",
        }

    def _quote(self, encoded_symbol: str) -> dict[str, Any]:
        response = self._session.get(
            self._YAHOO_CHART.format(symbol=encoded_symbol),
            params={"range": "5d", "interval": "1d"},
            timeout=float(self._settings.public_macro_timeout_seconds),
        )
        response.raise_for_status()
        result = (((response.json().get("chart") or {}).get("result") or [None])[0] or {})
        meta = result.get("meta") or {}
        latest = self._number(meta.get("regularMarketPrice"))
        previous = self._number(meta.get("chartPreviousClose") or meta.get("previousClose"))
        if latest is None or previous in {None, 0.0}:
            raise RuntimeError("quote lacks latest/previous close")
        return {
            "value": latest,
            "previous_close": previous,
            "change_pct": round(((latest - previous) / previous) * 100.0, 4),
            "source": "public_market_chart",
        }

    def _fii_dii(self) -> dict[str, Any]:
        response = self._session.get(
            self._NSE_FLOWS,
            timeout=float(self._settings.public_macro_timeout_seconds),
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("unexpected FII/DII response")
        parsed: dict[str, float] = {}
        trade_date = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            category = str(row.get("category") or "").upper()
            value = self._number(row.get("netValue") or row.get("net_value"))
            if value is None:
                continue
            if "FII" in category or "FPI" in category:
                parsed["fii_net"] = value
            elif "DII" in category:
                parsed["dii_net"] = value
            trade_date = trade_date or row.get("date")
        if "fii_net" not in parsed or "dii_net" not in parsed:
            raise RuntimeError("FII/DII values unavailable")
        return {**parsed, "trade_date": trade_date, "source": "nse"}

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(str(value).replace(",", "")) if value not in {None, ""} else None
        except (TypeError, ValueError):
            return None
