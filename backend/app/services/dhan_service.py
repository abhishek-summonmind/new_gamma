from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import time as time_module
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import DhanSettings, Settings, SYMBOL_CONFIG, get_settings, normalize_market_symbol
from ..constants.nifty50 import NIFTY50_SYMBOLS
from .dhan_config_service import DhanConfigService
from .option_types import OptionChainSnapshot, OptionContract

logger = logging.getLogger(__name__)

GROWW_INDEX_MAP: dict[str, dict[str, Any]] = {
    "NIFTY 50": {
        "underlying": "NIFTY", "exchange": "NSE", "segment": "CASH",
        "equity": "NSE-NIFTY", "historical_symbols": ("NSE-NIFTY",), "historical_trading_symbol": "NIFTY",
    },
    "BANK NIFTY": {
        "underlying": "BANKNIFTY", "exchange": "NSE", "segment": "CASH",
        "equity": "NSE-BANKNIFTY",
        "historical_symbols": ("NSE-BANKNIFTY", "NSE-BANK_NIFTY", "NSE-NIFTY_BANK"), "historical_trading_symbol": "BANKNIFTY",
    },
    "FINNIFTY": {
        "underlying": "FINNIFTY", "exchange": "NSE", "segment": "CASH",
        "equity": "NSE-NIFTY_FIN_SERVICE",
        "historical_symbols": ("NSE-NIFTY_FIN_SERVICE", "NSE-FINNIFTY"), "historical_trading_symbol": "FINNIFTY",
    },
    "SENSEX": {
        "underlying": "SENSEX", "exchange": "BSE", "segment": "CASH",
        "equity": "BSE-SENSEX", "historical_symbols": ("BSE-SENSEX",), "historical_trading_symbol": "SENSEX",
    },
}
GROWW_INDEX_MAP.update({key: {"underlying": value["option_underlying"], "exchange": value["exchange"], "segment": value["segment"], "equity": value["groww_symbol"], "historical_symbols": (value["groww_symbol"],), "historical_trading_symbol": key} for key, value in SYMBOL_CONFIG.items() if key not in GROWW_INDEX_MAP})

_INSTRUMENT_CACHE_THREAD_LOCK = Lock()


@contextmanager
def _exclusive_file_lock(lock_path: str):
    """Cross-process lock used while validating/replacing the instrument cache."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+b") as handle:
        handle.seek(0)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            handle.seek(0)
            unlock()

class DhanService:
    """Low-level Dhan REST service with retries, timeout handling, and safe parsers."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dhan_settings: DhanSettings | None = None,
    ):
        self._settings = settings
        self._config_service = DhanConfigService(settings)
        self._dhan = dhan_settings or (settings.dhan if settings is not None else get_settings().dhan)
        self._groww = settings if settings is not None else self._dhan
        self._groww_api_version = settings.groww_api_version if settings is not None else "1.0"
        self._base_url = self._dhan.base_url.rstrip("/")
        self._session = self._build_session()
        self._groww_generated_access_token = ""
        # Last-good provider responses are deliberately keyed by the complete
        # contract identity.  This prevents a transient failure from ever
        # borrowing data from another symbol, exchange, expiry, or timeframe.
        self._groww_option_chain_cache: dict[
            tuple[str, str, date], tuple[datetime, OptionChainSnapshot]
        ] = {}
        self._groww_candle_cache: dict[
            tuple[str, str, int], tuple[datetime, pd.DataFrame]
        ] = {}
        self._last_groww_option_chain_identity: tuple[str, str, date] | None = None

    @property
    def configured(self) -> bool:
        if self._provider == "groww":
            return bool(
                str(self._groww.groww_access_token or "").strip()
                or (
                    str(self._groww.groww_api_key or "").strip()
                    and str(self._groww.groww_api_secret or "").strip()
                )
            )
        access_token = self._config_service.get_effective_access_token()
        return bool(access_token and access_token.strip() and self._dhan.client_id.strip())

    @property
    def _provider(self) -> str:
        return str(getattr(self._dhan, "provider", "dhan") or "dhan").strip().lower()

    def get_nifty50_symbols(self) -> tuple[str, ...]:
        if self._settings is not None:
            return tuple(symbol.upper() for symbol in self._settings.nifty_symbols)
        return tuple(NIFTY50_SYMBOLS)

    def get_ohlc(
        self,
        symbol: str,
        timeframe: str,
        *,
        now_market: datetime | None = None,
        lookback_days: int | None = None,
    ) -> pd.DataFrame:
        """Return OHLCV frame indexed by UTC timestamp. Returns empty frame on failure."""
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        try:
            interval = self._parse_timeframe_to_minutes(timeframe)
            if self._provider == "groww":
                current = now_market or datetime.now()
                lookback = int(lookback_days or (self._settings.intraday_fetch_days if self._settings else 2))
                fetch_from = current - timedelta(days=max(1, lookback))
                return self.get_groww_intraday_ohlc(
                    trading_symbol=symbol,
                    from_date=fetch_from,
                    to_date=current,
                    interval_minutes=interval,
                )

            security_id, exchange_segment, instrument = self._resolve_symbol_context(symbol)

            current = now_market or datetime.now()
            lookback = int(lookback_days or (self._settings.intraday_fetch_days if self._settings else 2))
            fetch_from = current - timedelta(days=max(1, lookback))

            payload = {
                "securityId": str(security_id),
                "exchangeSegment": exchange_segment,
                "instrument": instrument,
                "interval": str(interval),
                "oi": False,
                "fromDate": fetch_from.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": current.strftime("%Y-%m-%d %H:%M:%S"),
            }

            data = self.post_json("/charts/intraday", payload)
            parsed = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
            if not isinstance(parsed, dict):
                raise RuntimeError("Unexpected intraday payload received from Dhan.")

            open_values = parsed.get("open") or []
            high_values = parsed.get("high") or []
            low_values = parsed.get("low") or []
            close_values = parsed.get("close") or []
            volume_values = parsed.get("volume") or []
            timestamp_values = parsed.get("timestamp") or []

            lengths = {
                len(open_values),
                len(high_values),
                len(low_values),
                len(close_values),
                len(volume_values),
                len(timestamp_values),
            }
            if len(lengths) != 1 or not timestamp_values:
                raise RuntimeError("Intraday candle arrays are empty or inconsistent.")

            unit = "ms" if max(timestamp_values) > 9_999_999_999 else "s"
            timestamps = pd.to_datetime(timestamp_values, unit=unit, utc=True, errors="coerce")

            frame = pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "open": pd.to_numeric(open_values, errors="coerce"),
                    "high": pd.to_numeric(high_values, errors="coerce"),
                    "low": pd.to_numeric(low_values, errors="coerce"),
                    "close": pd.to_numeric(close_values, errors="coerce"),
                    "volume": pd.Series(pd.to_numeric(volume_values, errors="coerce")).fillna(0.0),
                }
            )
            frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
            if frame.empty:
                raise RuntimeError("No valid OHLC rows parsed from Dhan response.")

            frame = frame.sort_values("timestamp").set_index("timestamp")
            return frame
        except Exception as exc:  # noqa: BLE001
            logger.warning("OHLC fetch failed for %s (%s): %s", symbol, timeframe, exc)
            return empty

    def get_option_market_depth(
        self,
        underlying: str,
        contract: OptionContract,
    ) -> dict[str, Any]:
        """Fetch one uncached top-of-book snapshot for an exact option contract."""
        symbol = normalize_market_symbol(underlying)
        config = SYMBOL_CONFIG.get(symbol) or {}
        security_id = str(contract.security_id or "").strip()
        if not security_id:
            raise RuntimeError("Option contract security/trading symbol is missing")

        if self._provider == "groww":
            response = self.get_json(
                "/live-data/quote",
                params={
                    "exchange": str(config.get("option_exchange") or "NSE").upper(),
                    "segment": str(getattr(self._settings, "groww_fno_segment", "FNO") or "FNO").upper(),
                    "trading_symbol": security_id,
                },
            )
            payload = response.get("payload") if isinstance(response, dict) else None
            if not isinstance(payload, dict):
                raise RuntimeError("Groww quote response has no payload")
            source = "groww_live_data_quote"
        else:
            exchange = str(config.get("option_exchange") or "NSE").upper()
            exchange_segment = "BSE_FNO" if exchange == "BSE" else self._dhan.options_exchange_segment
            response = self.post_json(
                "/marketfeed/quote",
                {exchange_segment: [int(security_id)]},
            )
            data = response.get("data") if isinstance(response, dict) else None
            segment_payload = data.get(exchange_segment) if isinstance(data, dict) else None
            payload = segment_payload.get(security_id) if isinstance(segment_payload, dict) else None
            if payload is None and isinstance(segment_payload, dict):
                payload = segment_payload.get(int(security_id))
            if not isinstance(payload, dict):
                raise RuntimeError("Dhan quote response has no contract payload")
            source = "dhan_marketfeed_quote"

        depth = payload.get("depth") if isinstance(payload.get("depth"), dict) else {}
        buy_rows = depth.get("buy") if isinstance(depth.get("buy"), list) else []
        sell_rows = depth.get("sell") if isinstance(depth.get("sell"), list) else []
        bid_row = buy_rows[0] if buy_rows and isinstance(buy_rows[0], dict) else {}
        ask_row = sell_rows[0] if sell_rows and isinstance(sell_rows[0], dict) else {}
        bid_price = self._optional_float(
            payload.get("bid_price") or payload.get("best_bid_price") or bid_row.get("price")
        )
        ask_price = self._optional_float(
            payload.get("offer_price") or payload.get("ask_price")
            or payload.get("best_ask_price") or ask_row.get("price")
        )
        bid_qty = self._optional_float(
            payload.get("bid_quantity") or payload.get("bid_qty") or bid_row.get("quantity")
        )
        ask_qty = self._optional_float(
            payload.get("offer_quantity") or payload.get("ask_quantity")
            or payload.get("ask_qty") or ask_row.get("quantity")
        )
        missing = [
            name
            for name, value in (
                ("bid_price", bid_price),
                ("ask_price", ask_price),
                ("bid_quantity", bid_qty),
                ("ask_quantity", ask_qty),
            )
            if value is None
        ]
        if missing:
            raise RuntimeError(f"Live top-of-book fields missing: {', '.join(missing)}")
        return {
            "bid_price": bid_price,
            "ask_price": ask_price,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "observed_at": datetime.now(tz=ZoneInfo(self._settings.market_timezone if self._settings else "Asia/Kolkata")),
            "source": source,
        }

    def get_option_chain(self, symbol: str, *, depth: int | None = None) -> OptionChainSnapshot:
        """
        Return ATM CE/PE option chain snapshot for the given underlying symbol using
        the manually-configured expiry date from DHAN_OPTION_EXPIRY.
        
        The expiry date is retrieved from:
        1. Database configuration (if set via API endpoint)
        2. Environment variable (DHAN_OPTION_EXPIRY)
        3. DhanSettings.option_expiry (if set at startup)
        
        The retrieved expiry is validated to ensure it matches an available option expiry from Dhan.
        """
        target = symbol.strip().upper()
        if self._provider == "groww":
            return self._get_groww_option_chain(target, depth=depth)

        security_id, exchange_segment = self._resolve_option_chain_context(target)

        # Query available expiries from Dhan to validate the configured one exists
        expiry_rows = self.post_json(
            "/optionchain/expirylist",
            {
                "UnderlyingScrip": int(security_id),
                "UnderlyingSeg": exchange_segment,
            },
        )
        expiry_data = expiry_rows.get("data") if isinstance(expiry_rows, dict) else None
        if not isinstance(expiry_data, list) or not expiry_data:
            raise RuntimeError("No expiries returned by Dhan option-chain expirylist.")

        parsed_expiries: list[date] = []
        for row in expiry_data:
            try:
                parsed_expiries.append(date.fromisoformat(str(row)))
            except ValueError:
                continue
        if not parsed_expiries:
            raise RuntimeError("Unable to parse option-chain expiry dates from Dhan.")

        if self._uses_auto_weekly_expiry(target):
            expiry = self._next_available_expiry(available_expiries=parsed_expiries)
            logger.info(
                "Using next weekly Dhan expiry for %s: %s",
                target,
                expiry.isoformat(),
            )
        else:
            try:
                configured_expiry = self._config_service.get_option_expiry()
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc

            logger.debug(
                "Fetching option chain for %s using configured expiry: %s",
                target,
                configured_expiry.isoformat(),
            )
            expiry = self._nearest_available_expiry(
                configured_expiry=configured_expiry,
                available_expiries=parsed_expiries,
            )
            if expiry != configured_expiry:
                logger.warning(
                    "Configured Dhan expiry unavailable; using nearest valid expiry "
                    "underlying=%s configured=%s resolved=%s",
                    target,
                    configured_expiry.isoformat(),
                    expiry.isoformat(),
                )

        chain_payload = self.post_json(
            "/optionchain",
            {
                "UnderlyingScrip": int(security_id),
                "UnderlyingSeg": exchange_segment,
                "Expiry": expiry.isoformat(),
            },
        )
        data = chain_payload.get("data") if isinstance(chain_payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("Invalid option-chain payload from Dhan.")

        logger.info(
            "Option chain fetched for %s with expiry: %s (spot_price fetched)",
            target,
            expiry.isoformat(),
        )

        option_chain = data.get("oc") or {}
        if not isinstance(option_chain, dict):
            raise RuntimeError("Option-chain response does not contain 'oc'.")

        spot_price = self._to_float(data.get("last_price") or data.get("lastPrice"))
        contracts: list["OptionContract"] = []

        # Import locally to avoid circular import at module load time.
        from .dhan_client import DhanClient as _DhanClient

        for strike_key, strike_payload in option_chain.items():
            if not isinstance(strike_payload, dict):
                continue

            try:
                strike_value = float(strike_key)
            except (TypeError, ValueError):
                continue

            ce_data = strike_payload.get("ce") if isinstance(strike_payload.get("ce"), dict) else {}
            pe_data = strike_payload.get("pe") if isinstance(strike_payload.get("pe"), dict) else {}

            ce_contract = _DhanClient._parse_contract_payload(ce_data, strike=strike_value, option_type="CE")
            pe_contract = _DhanClient._parse_contract_payload(pe_data, strike=strike_value, option_type="PE")

            if ce_contract is not None:
                contracts.append(ce_contract)
            if pe_contract is not None:
                contracts.append(pe_contract)

        if not contracts:
            raise RuntimeError("No option contracts were available in Dhan option chain response.")

        all_strikes = sorted({row.strike for row in contracts})
        atm_strike = int(round(min(all_strikes, key=lambda strike: abs(strike - spot_price))))

        chain_depth = depth if depth is not None else self._dhan.option_chain_depth
        if normalize_market_symbol(target) in {"NIFTY 50", "SENSEX"}:
            chain_depth = max(4, int(chain_depth))
        selected_strikes = _DhanClient._slice_strikes_around_atm(all_strikes, atm_strike, chain_depth)
        selected_set = set(selected_strikes)

        filtered_contracts = tuple(
            sorted(
                (row for row in contracts if row.strike in selected_set),
                key=lambda row: (row.strike, row.option_type),
            )
        )

        return OptionChainSnapshot(
            underlying=target,
            spot_price=spot_price,
            atm_strike=atm_strike,
            expiry_date=expiry,
            snapshot_time=datetime.utcnow(),
            contracts=filtered_contracts,
        )

    def get_groww_intraday_ohlc(
        self,
        *,
        trading_symbol: str,
        from_date: datetime,
        to_date: datetime,
        interval_minutes: int = 1,
    ) -> pd.DataFrame:
        context = self._groww_index_context(trading_symbol)
        exchange = str(context.get("exchange") or self._groww.groww_exchange).upper()
        segment = str(context.get("segment") or self._groww.groww_cash_segment).upper()
        primary_symbol = self._groww_equity_symbol(trading_symbol)
        candidate_symbols = tuple(dict.fromkeys(context.get("historical_symbols") or (primary_symbol,)))
        if primary_symbol not in candidate_symbols:
            candidate_symbols = (primary_symbol, *candidate_symbols)
        from_date, to_date = self._normalize_groww_session_range(from_date, to_date)
        cache_key = (exchange, normalize_market_symbol(trading_symbol), int(interval_minutes))
        errors: list[str] = []
        cache_fallback_allowed = True
        compatibility_attempted = False
        trading_code = str(context.get("historical_trading_symbol") or context.get("underlying") or "").upper()

        for groww_symbol in candidate_symbols:
            params = {
                "exchange": exchange,
                "segment": segment,
                "groww_symbol": groww_symbol,
                "start_time": from_date.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": to_date.strftime("%Y-%m-%d %H:%M:%S"),
                "candle_interval": f"{int(interval_minutes)}minute",
            }
            try:
                data = self.get_json("/historical/candles", params=params)
            except RuntimeError as exc:
                message = str(exc)
                logger.warning(
                    "GROWW_PRIMARY_FAILED endpoint=/historical/candles symbol=%s interval=%s error=%s",
                    groww_symbol, params["candle_interval"], message,
                )
                if self._is_http_404(message) and trading_code:
                    compatibility_attempted = True
                    try:
                        frame = self._get_groww_compatibility_candles(
                            exchange=exchange,
                            segment=segment,
                            trading_code=trading_code,
                            from_date=from_date,
                            to_date=to_date,
                            interval_minutes=interval_minutes,
                        )
                    except RuntimeError as compatibility_exc:
                        if not (
                            self._is_http_404(str(compatibility_exc))
                            or self._is_transient_groww_error(str(compatibility_exc))
                        ):
                            cache_fallback_allowed = False
                        errors.append(f"{groww_symbol}@primary: {message}")
                        errors.append(f"{trading_code}@range: {compatibility_exc}")
                        continue
                    self._cache_groww_candles(cache_key, frame)
                    return frame.copy(deep=True)
                if self._is_transient_groww_error(message):
                    time_module.sleep(0.75)
                    try:
                        data = self.get_json("/historical/candles", params=params)
                    except RuntimeError as retry_exc:
                        if not self._is_transient_groww_error(str(retry_exc)):
                            cache_fallback_allowed = False
                        errors.append(f"{groww_symbol}: {retry_exc}")
                        continue
                else:
                    cache_fallback_allowed = False
                    errors.append(f"{groww_symbol}: {message}")
                    continue
            shape = self._describe_groww_payload(data)
            logger.info(
                "Groww historical response: symbol=%s exchange=%s segment=%s interval=%s "
                "start_time=%s end_time=%s response_shape=%s",
                groww_symbol, exchange, segment, params["candle_interval"],
                params["start_time"], params["end_time"], shape,
            )
            candles = self._extract_groww_candles(data)
            if not candles:
                errors.append(f"{groww_symbol}: empty ({shape})")
                continue
            frame = self._parse_groww_candles(candles)
            if frame.empty:
                errors.append(f"{groww_symbol}: no valid rows ({shape})")
                continue
            logger.info(
                "Groww historical candles parsed: symbol=%s exchange=%s segment=%s candle_count=%s",
                groww_symbol, exchange, segment, len(frame),
            )
            self._cache_groww_candles(cache_key, frame)
            return frame.copy(deep=True)

        # Generic compatibility path for indices not populated by the newer
        # backtesting endpoint. Uses broker metadata rather than symbol conditionals.
        if trading_code and not compatibility_attempted:
            compatibility_attempted = True
            try:
                frame = self._get_groww_compatibility_candles(
                    exchange=exchange,
                    segment=segment,
                    trading_code=trading_code,
                    from_date=from_date,
                    to_date=to_date,
                    interval_minutes=interval_minutes,
                )
            except RuntimeError as exc:
                if not (self._is_http_404(str(exc)) or self._is_transient_groww_error(str(exc))):
                    cache_fallback_allowed = False
                errors.append(f"{trading_code}@range: {exc}")
            else:
                self._cache_groww_candles(cache_key, frame)
                return frame.copy(deep=True)

        if cache_fallback_allowed:
            cached = self._cached_groww_candles(cache_key)
            if cached is not None:
                return cached
        raise RuntimeError(
            "Groww historical candles unavailable: "
            f"symbol={normalize_market_symbol(trading_symbol)} exchange={exchange} segment={segment} "
            f"interval={int(interval_minutes)}minute attempts={'; '.join(errors) or 'none'}"
        )

    def _get_groww_compatibility_candles(
        self,
        *,
        exchange: str,
        segment: str,
        trading_code: str,
        from_date: datetime,
        to_date: datetime,
        interval_minutes: int,
    ) -> pd.DataFrame:
        legacy_params = {
            "exchange": exchange,
            "segment": segment,
            "trading_symbol": trading_code,
            "start_time": from_date.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": to_date.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_in_minutes": str(int(interval_minutes)),
        }
        data = self.get_json("/historical/candle/range", params=legacy_params)
        shape = self._describe_groww_payload(data)
        frame = self._parse_groww_candles(self._extract_groww_candles(data))
        if frame.empty:
            raise RuntimeError(f"empty Groww compatibility response ({shape})")
        logger.info(
            "Groww historical candles parsed: symbol=%s exchange=%s segment=%s "
            "candle_count=%s endpoint=range",
            trading_code, exchange, segment, len(frame),
        )
        return frame

    def _cache_groww_candles(
        self,
        key: tuple[str, str, int],
        frame: pd.DataFrame,
    ) -> None:
        if frame.empty:
            return
        self._groww_candle_cache[key] = (datetime.utcnow(), frame.copy(deep=True))

    def _cached_groww_candles(self, key: tuple[str, str, int]) -> pd.DataFrame | None:
        cached = self._groww_candle_cache.get(key)
        if cached is None:
            return None
        cached_at, frame = cached
        age_seconds = max(0.0, (datetime.utcnow() - cached_at).total_seconds())
        stale_after = self._groww_cache_stale_after_seconds()
        if age_seconds > stale_after:
            logger.warning(
                "GROWW_CACHE_STALE kind=candles exchange=%s symbol=%s timeframe=%sminute "
                "cache_age_seconds=%.1f stale_after_seconds=%s",
                key[0], key[1], key[2], age_seconds, stale_after,
            )
        logger.warning(
            "GROWW_CACHE_FALLBACK kind=candles exchange=%s symbol=%s timeframe=%sminute "
            "cache_age_seconds=%.1f rows=%s",
            key[0], key[1], key[2], age_seconds, len(frame),
        )
        return frame.copy(deep=True)

    @staticmethod
    def _is_http_404(message: str) -> bool:
        return "http 404" in str(message or "").lower()

    @staticmethod
    def _is_transient_groww_error(
        message: str,
        *,
        allow_validated_missing_underlying: bool = False,
    ) -> bool:
        normalized = str(message or "").lower()
        # The option-chain API can briefly return HTTP 404 + GA000 for an
        # underlying that the instrument master has already validated.  Do
        # not apply this exception to unvalidated identities or other APIs.
        if "underlying not found" in normalized:
            return bool(allow_validated_missing_underlying and "ga000" in normalized)
        if "http 404" in normalized:
            return False
        markers = (
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "ga000",
            "ga003",
            "internal server error",
            "unable to serve request currently",
        )
        return any(marker in normalized for marker in markers)

    def _get_groww_option_chain_with_retry(
        self,
        *,
        endpoint: str,
        target: str,
        underlying: str,
        expiry: date,
        identity_validated: bool = False,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        # Three attempts is the provider policy today. Keep the argument for
        # compatibility while enforcing the required minimum and bounded load.
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                data = self.get_json(endpoint, params={"expiry_date": expiry.isoformat()})
                if attempt > 1:
                    logger.info(
                        "GROWW_RETRY_RECOVERED endpoint=option-chain symbol=%s underlying=%s "
                        "expiry=%s attempt=%s/%s",
                        target,
                        underlying,
                        expiry.isoformat(),
                        attempt,
                        attempts,
                    )
                return data
            except RuntimeError as exc:
                message = str(exc)
                transient = self._is_transient_groww_error(
                    message,
                    allow_validated_missing_underlying=identity_validated,
                )
                if not transient or attempt >= attempts:
                    logger.error(
                        "GROWW_PRIMARY_FAILED endpoint=option-chain symbol=%s underlying=%s expiry=%s "
                        "attempt=%s/%s transient=%s error=%s",
                        target,
                        underlying,
                        expiry.isoformat(),
                        attempt,
                        attempts,
                        str(transient).lower(),
                        message,
                    )
                    raise
                delay_seconds = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "GROWW_PRIMARY_FAILED endpoint=option-chain retrying=true symbol=%s "
                    "underlying=%s expiry=%s "
                    "attempt=%s/%s delay_seconds=%.1f error=%s",
                    target,
                    underlying,
                    expiry.isoformat(),
                    attempt,
                    attempts,
                    delay_seconds,
                    message,
                )
                time_module.sleep(delay_seconds)

        raise RuntimeError("Groww option-chain retry loop exited unexpectedly.")

    @classmethod
    def _extract_groww_candles(cls, data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for container_key in ("payload", "data", "result", "response"):
            container = data.get(container_key)
            if isinstance(container, list):
                return container
            if isinstance(container, dict):
                for candles_key in ("candles", "candle_data", "candleData", "ohlc", "items"):
                    candles = container.get(candles_key)
                    if isinstance(candles, list):
                        return candles
        for candles_key in ("candles", "candle_data", "candleData", "ohlc", "items"):
            candles = data.get(candles_key)
            if isinstance(candles, list):
                return candles
        return []

    @classmethod
    def _parse_groww_candles(cls, candles: list[Any]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for candle in candles:
            if isinstance(candle, (list, tuple)) and len(candle) >= 5:
                volume = candle[5] if len(candle) > 5 else 0
                row = {
                    "timestamp": cls._parse_groww_timestamp(candle[0]),
                    "open": cls._to_float(candle[1]), "high": cls._to_float(candle[2]),
                    "low": cls._to_float(candle[3]), "close": cls._to_float(candle[4]),
                    "volume": cls._to_float(volume),
                }
            elif isinstance(candle, dict):
                row = {
                    "timestamp": cls._parse_groww_timestamp(
                        candle.get("timestamp") or candle.get("time") or candle.get("date") or candle.get("t")
                    ),
                    "open": cls._to_float(candle.get("open") if candle.get("open") is not None else candle.get("o")),
                    "high": cls._to_float(candle.get("high") if candle.get("high") is not None else candle.get("h")),
                    "low": cls._to_float(candle.get("low") if candle.get("low") is not None else candle.get("l")),
                    "close": cls._to_float(candle.get("close") if candle.get("close") is not None else candle.get("c")),
                    "volume": cls._to_float(candle.get("volume") if candle.get("volume") is not None else candle.get("v")),
                }
            else:
                continue
            rows.append(row)
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame
        frame = frame.dropna(subset=["timestamp"])
        valid_prices = (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
        frame = frame.loc[valid_prices]
        if frame.empty:
            return frame
        return frame.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").set_index("timestamp")

    @staticmethod
    def _describe_groww_payload(data: Any) -> str:
        if not isinstance(data, dict):
            return type(data).__name__
        parts = [f"top_keys={sorted(data.keys())}"]
        for key in ("payload", "data", "result", "response"):
            value = data.get(key)
            if isinstance(value, dict):
                parts.append(f"{key}_keys={sorted(value.keys())}")
            elif value is not None:
                parts.append(f"{key}_type={type(value).__name__}")
        return " ".join(parts)
    def _get_groww_option_chain(self, target: str, *, depth: int | None = None) -> OptionChainSnapshot:
        context = self._groww_index_context(target)
        underlying = context["underlying"] if context else self._groww_underlying_symbol(target)
        exchange = str(context["exchange"] if context else self._groww.groww_exchange).upper()
        if self._uses_auto_weekly_expiry(target):
            requested_expiry = None
            expiry = self._next_available_expiry(available_expiries=self._get_groww_fno_expiries(underlying))
            logger.info(
                "Using next weekly Groww expiry for %s/%s: %s",
                target,
                underlying,
                expiry.isoformat(),
            )
        elif self._uses_current_month_monthly_expiry(target):
            requested_expiry = None
            expiry = self.resolve_groww_stock_option_expiry(
                underlying=underlying,
                today=datetime.now(ZoneInfo(self._settings.market_timezone)).date(),
            )
            logger.info(
                "Using current-month Groww stock expiry for %s/%s: %s",
                target,
                underlying,
                expiry.isoformat(),
            )
        else:
            try:
                expiry = self._config_service.get_option_expiry()
            except ValueError as exc:
                raise RuntimeError(str(exc).replace("DHAN_OPTION_EXPIRY", "GROWW_OPTION_EXPIRY")) from exc

            requested_expiry = expiry
            expiry = self._validate_groww_option_expiry(underlying=underlying, expiry=expiry)

        self._validate_groww_option_identity(
            exchange=exchange,
            underlying=underlying,
            expiry=expiry,
        )
        cache_key = (exchange, str(underlying).strip().upper(), expiry)
        self._last_groww_option_chain_identity = cache_key
        try:
            data = self._get_groww_option_chain_with_retry(
                endpoint=f"/option-chain/exchange/{exchange}/underlying/{underlying}",
                target=target,
                underlying=underlying,
                expiry=expiry,
                identity_validated=True,
            )
        except RuntimeError as exc:
            if self._is_transient_groww_error(
                str(exc),
                allow_validated_missing_underlying=True,
            ):
                cached = self._cached_groww_option_chain(cache_key)
                if cached is not None:
                    return cached
            raise
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid option-chain payload from Groww.")
        strikes = payload.get("strikes")
        if not isinstance(strikes, dict):
            raise RuntimeError("Groww option-chain response does not contain strikes.")
        logger.info(
            "Groww option chain fetched: symbol=%s underlying=%s expiry=%s strike_count=%s",
            target,
            underlying,
            expiry.isoformat(),
            len(strikes),
        )

        spot_price = self._to_float(payload.get("underlying_ltp") or payload.get("underlyingLtp"))
        contracts: list[OptionContract] = []
        for strike_key, strike_payload in strikes.items():
            if not isinstance(strike_payload, dict) or not self._can_float(strike_key):
                continue
            strike_value = float(strike_key)
            for option_type in ("CE", "PE"):
                leg = strike_payload.get(option_type)
                if isinstance(leg, dict):
                    contract = self._parse_groww_contract(leg, strike=strike_value, option_type=option_type)
                    if contract is not None:
                        contracts.append(contract)

        if not contracts:
            raise RuntimeError("No option contracts were available in Groww option chain response.")
        all_strikes = sorted({row.strike for row in contracts})
        atm_strike = int(round(min(all_strikes, key=lambda strike: abs(strike - spot_price))))

        from .dhan_client import DhanClient as _DhanClient

        chain_depth = depth if depth is not None else self._dhan.option_chain_depth
        if normalize_market_symbol(target) in {"NIFTY 50", "SENSEX"}:
            chain_depth = max(4, int(chain_depth))
        selected_strikes = _DhanClient._slice_strikes_around_atm(all_strikes, atm_strike, chain_depth)
        selected_set = set(selected_strikes)
        filtered_contracts = tuple(
            sorted(
                (row for row in contracts if row.strike in selected_set),
                key=lambda row: (row.strike, row.option_type),
            )
        )

        snapshot = OptionChainSnapshot(
            underlying=target,
            spot_price=spot_price,
            atm_strike=atm_strike,
            expiry_date=expiry,
            snapshot_time=datetime.utcnow(),
            contracts=filtered_contracts,
            requested_expiry=requested_expiry,
            fallback_used=requested_expiry is not None and expiry != requested_expiry,
        )
        # Cache only a fully parsed, non-empty snapshot. Failed and empty API
        # responses can therefore never evict the last known-good contract.
        self._groww_option_chain_cache[cache_key] = (datetime.utcnow(), snapshot)
        return snapshot

    def _validate_groww_option_identity(
        self,
        *,
        exchange: str,
        underlying: str,
        expiry: date,
    ) -> None:
        available_expiries = self._get_groww_fno_expiries(underlying)
        if expiry in available_expiries:
            return
        logger.error(
            "GROWW_INSTRUMENT_INVALID exchange=%s underlying=%s expiry=%s available_expiries=%s",
            exchange,
            underlying,
            expiry.isoformat(),
            ",".join(row.isoformat() for row in sorted(available_expiries)) or "none",
        )
        raise RuntimeError(
            "Groww instrument master does not contain requested option identity: "
            f"exchange={exchange} underlying={underlying} expiry={expiry.isoformat()}"
        )

    def _cached_groww_option_chain(
        self,
        key: tuple[str, str, date],
    ) -> OptionChainSnapshot | None:
        cached = self._groww_option_chain_cache.get(key)
        if cached is None:
            return None
        cached_at, snapshot = cached
        age_seconds = max(0.0, (datetime.utcnow() - cached_at).total_seconds())
        stale_after = self._groww_cache_stale_after_seconds()
        if age_seconds > stale_after:
            logger.warning(
                "GROWW_CACHE_STALE kind=option-chain exchange=%s underlying=%s expiry=%s "
                "cache_age_seconds=%.1f stale_after_seconds=%s",
                key[0], key[1], key[2].isoformat(), age_seconds, stale_after,
            )
        logger.warning(
            "GROWW_CACHE_FALLBACK kind=option-chain exchange=%s underlying=%s expiry=%s "
            "cache_age_seconds=%.1f snapshot_time=%s contracts=%s",
            key[0], key[1], key[2].isoformat(), age_seconds,
            snapshot.snapshot_time.isoformat(), len(snapshot.contracts),
        )
        return replace(snapshot, fallback_used=True)

    def _groww_cache_stale_after_seconds(self) -> int:
        refresh_seconds = int(
            getattr(self._settings, "refresh_interval_seconds", 60)
            if self._settings is not None else 60
        )
        return max(60, refresh_seconds * 3)

    @property
    def last_groww_option_chain_identity(self) -> tuple[str, str, date] | None:
        return self._last_groww_option_chain_identity

    def get_json(self, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._provider != "groww":
            raise RuntimeError("GET JSON is currently supported for Groww provider only.")
        if not self.configured:
            raise RuntimeError(
                "Groww credentials are missing. Configure GROWW_ACCESS_TOKEN or "
                "both GROWW_API_KEY and GROWW_API_SECRET."
            )

        url = f"{self._groww.groww_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        request_params = params or {}
        headers = {
            "Authorization": f"Bearer {self._effective_access_token().strip()}",
            "Accept": "application/json",
            "X-API-VERSION": self._groww_api_version,
        }
        logger.info(
            "Groww request: method=GET endpoint=%s url=%s params=%s headers=%s",
            endpoint,
            url,
            request_params,
            {"Authorization": "Bearer ***", "Accept": headers["Accept"], "X-API-VERSION": headers["X-API-VERSION"]},
        )
        try:
            response = self._session.get(
                url,
                params=request_params,
                headers=headers,
                timeout=float(self._dhan.timeout_seconds),
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"Groww request timed out for {endpoint}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Groww request failed for {endpoint}: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None
        logger.info(
            "Groww response: endpoint=%s status_code=%s response_keys=%s",
            endpoint,
            response.status_code,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )

        if response.status_code >= 400:
            message = self._extract_error_message(data) or response.text or response.reason
            if response.status_code == 401:
                raise RuntimeError(
                    "Groww authentication failed. Update GROWW_ACCESS_TOKEN, or verify "
                    "GROWW_API_KEY/GROWW_API_SECRET and daily approval."
                )
            raise RuntimeError(
                f"Groww request failed for {endpoint}: HTTP {response.status_code} "
                f"params={params or {}} - {message}"
            )
        if data is None:
            raise RuntimeError(f"Groww returned non-JSON response for {endpoint}.")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Groww response type for {endpoint}: {type(data).__name__}")
        if str(data.get("status", "")).upper() == "FAILURE":
            message = self._extract_error_message(data) or "Unknown Groww API error."
            raise RuntimeError(f"Groww API returned failure for {endpoint}: {message}")
        return data

    def groww_post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._provider != "groww":
            raise RuntimeError("Groww order API requires DHAN_PROVIDER=groww.")
        if not self.configured:
            raise RuntimeError("Groww credentials are missing.")
        url = f"{self._groww.groww_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._effective_access_token().strip()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-VERSION": self._groww_api_version,
        }
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=float(self._dhan.timeout_seconds),
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"Groww request timed out for {endpoint}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Groww request failed for {endpoint}: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            message = self._extract_error_message(data) or response.text or response.reason
            raise RuntimeError(f"Groww request failed for {endpoint}: HTTP {response.status_code} - {message}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Groww returned invalid JSON for {endpoint}.")
        if str(data.get("status") or "").upper() == "FAILURE":
            raise RuntimeError(f"Groww API returned failure for {endpoint}: {self._extract_error_message(data)}")
        return data

    def post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Dhan credentials are missing. Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")

        access_token = self._config_service.get_effective_access_token() or self._dhan.access_token
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = {
            "access-token": access_token.strip(),
            "client-id": self._dhan.client_id.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=float(self._dhan.timeout_seconds),
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"Dhan request timed out for {endpoint}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Dhan request failed for {endpoint}: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if endpoint.rstrip("/").endswith("charts/intraday"):
            logger.info(
                "Dhan intraday request: endpoint=%s payload=%s response_status=%s response_keys=%s",
                endpoint,
                payload,
                response.status_code,
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            if isinstance(data, dict):
                payload_data = data.get("data") if isinstance(data.get("data"), dict) else data
                logger.info(
                    "Dhan intraday response metadata: data_keys=%s counts=%s",
                    list(payload_data.keys()) if isinstance(payload_data, dict) else None,
                    {
                        key: len(payload_data.get(key) or []) if isinstance(payload_data, dict) else None
                        for key in ("open", "high", "low", "close", "volume", "timestamp")
                    },
                )

        if response.status_code >= 400:
            message = self._extract_error_message(data) or response.text or response.reason
            message = str(message).strip()
            lowered = message.lower()

            error_code = str(data.get("errorCode", "")).strip().upper() if isinstance(data, dict) else ""
            error_type = str(data.get("errorType", "")).strip().lower() if isinstance(data, dict) else ""

            if response.status_code == 401 and (
                error_code == "DH-901"
                or "invalid_authentication" in error_type
                or "authentication failed" in lowered
                or "token invalid" in lowered
            ):
                raise RuntimeError(
                    "Dhan authentication failed. Update DHAN_ACCESS_TOKEN with a fresh token."
                )
            if response.status_code == 401 and "data api" in lowered and "subscribed" in lowered:
                raise RuntimeError(
                    "Dhan Data API access is not enabled for this account. Enable subscription in Dhan profile."
                )

            raise RuntimeError(f"Dhan request failed for {endpoint}: HTTP {response.status_code} - {message}")

        if data is None:
            raise RuntimeError(f"Dhan returned non-JSON response for {endpoint}.")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Dhan response type for {endpoint}: {type(data).__name__}")
        if str(data.get("status", "")).lower() in {"failure", "failed"}:
            message = self._extract_error_message(data) or "Unknown Dhan API error."
            raise RuntimeError(f"Dhan API returned failure for {endpoint}: {message}")

        return data

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(408, 425, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=40)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _effective_access_token(self) -> str:
        if self._provider == "groww":
            direct_token = str(self._groww.groww_access_token or "").strip()
            if direct_token:
                return direct_token
            if self._groww_generated_access_token:
                return self._groww_generated_access_token
            return self._generate_groww_access_token()
        database_token = self._config_service.get_effective_access_token()
        return (
            database_token
            or self._dhan.access_token
            or ""
        )

    def get_groww_access_token(self) -> str:
        """Return the configured/generated token for the official Groww SDK."""
        if self._provider != "groww":
            raise RuntimeError("Groww live feed requires the Groww market-data provider")
        return self._effective_access_token()

    def _generate_groww_access_token(self) -> str:
        api_key = str(self._groww.groww_api_key or "").strip()
        api_secret = str(self._groww.groww_api_secret or "").strip()
        if not api_key or not api_secret:
            return ""

        timestamp = str(int(time_module.time()))
        checksum = hashlib.sha256(f"{api_secret}{timestamp}".encode("utf-8")).hexdigest()
        url = f"{self._groww.groww_base_url.rstrip('/')}/token/api/access"
        try:
            response = self._session.post(
                url,
                json={
                    "key_type": "approval",
                    "checksum": checksum,
                    "timestamp": timestamp,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=float(self._dhan.timeout_seconds),
            )
        except requests.Timeout as exc:
            raise RuntimeError("Groww access-token generation timed out.") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Groww access-token generation failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400 or not isinstance(data, dict):
            message = self._extract_error_message(data) or response.text or response.reason
            raise RuntimeError(
                f"Groww access-token generation failed: HTTP {response.status_code} - {message}"
            )

        token = str(data.get("token") or data.get("access_token") or "").strip()
        if not token and isinstance(data.get("payload"), dict):
            token = str(data["payload"].get("token") or data["payload"].get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Groww access-token response did not contain a token.")

        self._groww_generated_access_token = token
        logger.info("Groww access token generated successfully from API key and secret.")
        return token

    def _groww_equity_symbol(self, symbol: str) -> str:
        context = self._groww_index_context(symbol)
        if context:
            return context["equity"]
        configured = str(self._groww.groww_equity_symbol or "").strip().upper()
        if configured:
            return configured
        exchange = str(self._groww.groww_exchange or "NSE").strip().upper()
        trading_symbol = str(symbol or "").strip().upper()
        if trading_symbol.startswith(f"{exchange}-"):
            return trading_symbol
        return f"{exchange}-{trading_symbol}"

    def _normalize_groww_session_range(self, from_date: datetime, to_date: datetime) -> tuple[datetime, datetime]:
        timezone_name = self._settings.market_timezone if self._settings is not None else "Asia/Kolkata"
        market_tz = ZoneInfo(timezone_name)
        start = self._as_market_datetime(from_date, market_tz)
        end = self._as_market_datetime(to_date, market_tz)
        open_time = self._parse_market_time(self._settings.market_open_time if self._settings is not None else "09:15")
        close_time = self._parse_market_time(self._settings.market_close_time if self._settings is not None else "15:30")

        # Preserve the requested multi-day lookback.  The old implementation
        # moved ``start`` to the current session open, which silently discarded
        # all previous sessions and left 15-minute RSI without enough candles.
        start_session_open = datetime.combine(start.date(), open_time, tzinfo=market_tz)
        normalized_start = start_session_open

        if end.time() < open_time:
            end_session_day = (end - timedelta(days=1)).date()
            normalized_end = datetime.combine(end_session_day, close_time, tzinfo=market_tz)
        else:
            end_session_open = datetime.combine(end.date(), open_time, tzinfo=market_tz)
            end_session_close = datetime.combine(end.date(), close_time, tzinfo=market_tz)
            normalized_end = min(max(end, end_session_open), end_session_close)
        if normalized_start >= normalized_end:
            normalized_start = datetime.combine(normalized_end.date(), open_time, tzinfo=market_tz)
        if (from_date, to_date) != (normalized_start, normalized_end):
            logger.info(
                "Groww historical time range normalized to NSE session: from=%s to=%s",
                normalized_start.isoformat(),
                normalized_end.isoformat(),
            )
        return normalized_start, normalized_end

    @staticmethod
    def _as_market_datetime(value: datetime, market_tz: ZoneInfo) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=market_tz)
        return value.astimezone(market_tz)

    @staticmethod
    def _parse_market_time(value: str) -> time:
        hours, minutes = str(value or "").split(":", 1)
        return time(int(hours), int(minutes))

    def _validate_groww_option_expiry(self, *, underlying: str, expiry: date) -> date:
        expiries = self._get_groww_fno_expiries(underlying)
        resolved = self._nearest_available_expiry(
            configured_expiry=expiry,
            available_expiries=expiries,
        )
        if resolved != expiry:
            logger.warning(
                "Configured Groww expiry unavailable; using nearest valid expiry "
                "underlying=%s configured=%s resolved=%s",
                underlying,
                expiry.isoformat(),
                resolved.isoformat(),
            )
        return resolved

    @staticmethod
    def _uses_auto_weekly_expiry(symbol: str) -> bool:
        return normalize_market_symbol(symbol) in {"NIFTY 50", "SENSEX"}

    @staticmethod
    def _is_index_symbol(symbol: str) -> bool:
        return normalize_market_symbol(symbol) in {"NIFTY 50", "SENSEX", "BANK NIFTY", "FINNIFTY"}

    @classmethod
    def _uses_current_month_monthly_expiry(cls, symbol: str) -> bool:
        normalized = normalize_market_symbol(symbol)
        return not cls._is_index_symbol(normalized) or normalized in {"BANK NIFTY", "FINNIFTY"}

    def resolve_groww_stock_option_expiry(self, *, underlying: str, today: date | None = None) -> date:
        current_day = today or date.today()
        year, month = self._stock_expiry_target_month(current_day)
        expiries = self._get_groww_fno_expiries(underlying)
        return self._monthly_stock_expiry(
            underlying=underlying,
            available_expiries=expiries,
            year=year,
            month=month,
            today=current_day,
        )

    @staticmethod
    def _stock_expiry_target_month(today: date) -> tuple[int, int]:
        """Stock options stay on the current calendar month while it is valid."""
        return today.year, today.month

    @staticmethod
    def _monthly_stock_expiry(
        *,
        underlying: str,
        available_expiries: list[date] | set[date] | tuple[date, ...],
        year: int,
        month: int,
        today: date | None = None,
    ) -> date:
        current_day = today or date.today()
        candidates = sorted(
            row for row in set(available_expiries)
            if row >= current_day and row.year == year and row.month == month
        )
        if not candidates:
            available = ", ".join(row.isoformat() for row in sorted(set(available_expiries))) or "none"
            raise RuntimeError(
                "No non-expired Groww stock option expiry is available for current month "
                f"{year:04d}-{month:02d}. Underlying: {underlying}. Available expiries: {available}"
            )
        return candidates[-1]

    @staticmethod
    def _next_available_expiry(
        *,
        available_expiries: list[date] | set[date] | tuple[date, ...],
        today: date | None = None,
    ) -> date:
        current_day = today or date.today()
        candidates = sorted({row for row in available_expiries if row >= current_day})
        if not candidates:
            available = ", ".join(row.isoformat() for row in sorted(set(available_expiries))) or "none"
            raise RuntimeError(
                "No non-expired option expiry is available. "
                f"Available expiries: {available}"
            )
        return candidates[0]

    @staticmethod
    def _nearest_available_expiry(
        *,
        configured_expiry: date,
        available_expiries: list[date] | set[date] | tuple[date, ...],
        today: date | None = None,
    ) -> date:
        """Resolve an unavailable manual expiry to the nearest non-expired expiry."""
        current_day = today or date.today()
        candidates = sorted({row for row in available_expiries if row >= current_day})
        if not candidates:
            available = ", ".join(row.isoformat() for row in sorted(set(available_expiries))) or "none"
            raise RuntimeError(
                "No non-expired option expiry is available. "
                f"Configured expiry: {configured_expiry.isoformat()}. Available expiries: {available}"
            )
        if configured_expiry in candidates:
            return configured_expiry
        return min(
            candidates,
            key=lambda row: (abs((row - configured_expiry).days), row),
        )

    def _get_groww_fno_expiries(self, underlying: str) -> set[date]:
        cache_dir = ".cache"
        if self._settings is not None:
            cache_dir = str(Path(__file__).resolve().parents[2] / ".cache")
        cache_path = os.path.join(cache_dir, "groww_instrument_master.csv")
        os.makedirs(cache_dir, exist_ok=True)
        frame = self._load_groww_master_frame()

        columns = {str(col).strip().lower(): col for col in frame.columns}
        underlying_norm = str(underlying or "").strip().upper()
        subset = frame[
            (frame[columns["underlying_symbol"]].fillna("").str.strip().str.upper() == underlying_norm)
            & (frame[columns["segment"]].fillna("").str.strip().str.upper() == self._groww.groww_fno_segment.upper())
        ]
        expiries: set[date] = set()
        for raw in subset[columns["expiry_date"]].dropna().unique():
            try:
                expiries.add(date.fromisoformat(str(raw).strip()))
            except ValueError:
                continue
        logger.info("Groww instrument master expiries resolved: underlying=%s count=%s", underlying_norm, len(expiries))
        if not expiries:
            logger.error(
                "GROWW_INSTRUMENT_INVALID underlying=%s reason=no_fno_expiries_in_instrument_master",
                underlying_norm,
            )
        return expiries

    def resolve_groww_cash_instrument(self, symbol: str) -> dict[str, str]:
        """Resolve a cash instrument from Groww's master; never invent tokens."""
        canonical = normalize_market_symbol(symbol)
        config = SYMBOL_CONFIG.get(canonical)
        if not config:
            raise RuntimeError(f"Unsupported market symbol: {symbol}")
        frame = self._load_groww_master_frame()
        columns = {str(col).strip().lower(): col for col in frame.columns}
        trading_col = columns.get("trading_symbol") or columns.get("symbol")
        token_col = columns.get("exchange_token") or columns.get("security_id")
        exchange_col = columns.get("exchange")
        segment_col = columns.get("segment")
        if not trading_col or not token_col or not exchange_col or not segment_col:
            raise RuntimeError("Groww instrument master lacks exchange, segment, trading_symbol or exchange_token columns.")
        trading_candidates = {canonical, str(config["groww_symbol"]).split("-", 1)[-1].upper()}
        if canonical == "NIFTY 50":
            trading_candidates.add("NIFTY")
        if canonical == "BANK NIFTY":
            trading_candidates.update({"BANKNIFTY", "BANK_NIFTY", "NIFTY_BANK"})
        rows = frame[
            frame[exchange_col].fillna("").str.upper().eq(config["exchange"])
            & frame[segment_col].fillna("").str.upper().eq(config["segment"])
            & frame[trading_col].fillna("").str.upper().isin(trading_candidates)
        ]
        if rows.empty:
            raise RuntimeError(f"Groww cash instrument not found for symbol: {canonical}")
        row = rows.iloc[0]
        return {"symbol": canonical, "exchange": config["exchange"], "segment": config["segment"], "trading_symbol": str(row[trading_col]), "groww_symbol": config["groww_symbol"], "exchange_token": str(row[token_col]), "instrument_type": str(row[columns.get("instrument_type", segment_col)])}

    def resolve_groww_option_instrument(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
    ) -> dict[str, Any]:
        """Resolve an executable option contract and its exchange constraints."""
        canonical = normalize_market_symbol(underlying)
        config = SYMBOL_CONFIG.get(canonical)
        if not config:
            raise RuntimeError(f"Unsupported option underlying: {underlying}")
        frame = self._load_groww_master_frame()
        cols = {str(col).strip().lower(): col for col in frame.columns}
        required = {"exchange", "segment", "trading_symbol", "exchange_token", "underlying_symbol", "expiry_date", "strike_price", "instrument_type", "lot_size", "tick_size"}
        missing = sorted(required - set(cols))
        if missing:
            raise RuntimeError(f"Groww instrument master lacks executable fields: {', '.join(missing)}")
        underlying_key = str(config["option_underlying"]).upper()
        numeric_strikes = pd.to_numeric(frame[cols["strike_price"]], errors="coerce")
        rows = frame[
            frame[cols["exchange"]].fillna("").str.upper().eq(str(config["option_exchange"]).upper())
            & frame[cols["segment"]].fillna("").str.upper().eq("FNO")
            & frame[cols["underlying_symbol"]].fillna("").str.upper().eq(underlying_key)
            & frame[cols["expiry_date"]].fillna("").eq(expiry.isoformat())
            & numeric_strikes.eq(float(strike))
            & frame[cols["instrument_type"]].fillna("").str.upper().eq(option_type.upper())
        ]
        if rows.empty:
            raise RuntimeError(
                f"Groww option instrument not found: {canonical} {expiry.isoformat()} {strike:g} {option_type.upper()}"
            )
        row = rows.iloc[0]
        lot_size = max(1, int(float(row[cols["lot_size"]])))
        tick_size = max(0.01, float(row[cols["tick_size"]]))
        return {
            "underlying": canonical,
            "exchange": str(row[cols["exchange"]]).upper(),
            "segment": str(row[cols["segment"]]).upper(),
            "trading_symbol": str(row[cols["trading_symbol"]]),
            "security_id": str(row[cols["exchange_token"]]),
            "lot_size": lot_size,
            "tick_size": tick_size,
            "buy_allowed": str(row[cols.get("buy_allowed", cols["segment"])]).strip().lower() not in {"0", "false"},
            "sell_allowed": str(row[cols.get("sell_allowed", cols["segment"])]).strip().lower() not in {"0", "false"},
        }

    def _load_groww_master_frame(self) -> pd.DataFrame:
        cache_dir = str(Path(__file__).resolve().parents[2] / ".cache") if self._settings is not None else ".cache"
        cache_path = os.path.join(cache_dir, "groww_instrument_master.csv")
        os.makedirs(cache_dir, exist_ok=True)
        with _INSTRUMENT_CACHE_THREAD_LOCK:
            with _exclusive_file_lock(f"{cache_path}.lock"):
                return self._load_or_refresh_groww_instrument_master(cache_path)

    def _load_or_refresh_groww_instrument_master(self, cache_path: str) -> pd.DataFrame:
        cached_error: str | None = None
        if os.path.exists(cache_path):
            try:
                return self._read_valid_groww_instrument_master(cache_path)
            except (pd.errors.EmptyDataError, pd.errors.ParserError, OSError, RuntimeError) as exc:
                cached_error = str(exc)
                logger.warning("Invalid Groww instrument cache; regenerating path=%s error=%s", cache_path, exc)
                try:
                    os.unlink(cache_path)
                except FileNotFoundError:
                    pass

        url = "https://growwapi-assets.groww.in/instruments/instrument.csv"
        try:
            response = self._session.get(
                url, params={}, headers={"Accept": "text/csv"}, timeout=float(self._dhan.timeout_seconds)
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Groww instrument master refresh failed; cache_error={cached_error or 'missing'}: {exc}"
            ) from exc
        if response.status_code >= 400 or not str(response.text or "").strip():
            raise RuntimeError(
                "Groww instrument master refresh returned invalid content: "
                f"status={response.status_code} cache_error={cached_error or 'missing'}"
            )

        frame = self._validate_groww_instrument_frame(pd.read_csv(io.StringIO(response.text), dtype=str))
        cache_dir = os.path.dirname(cache_path) or "."
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", delete=False,
                dir=cache_dir, prefix="groww_instrument_master.", suffix=".tmp",
            ) as handle:
                temp_path = handle.name
                handle.write(response.text)
                handle.flush()
                os.fsync(handle.fileno())
            # Validate the exact bytes before the atomic replacement.
            self._read_valid_groww_instrument_master(temp_path)
            os.replace(temp_path, cache_path)
            temp_path = None
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        logger.info("Groww instrument master cache refreshed path=%s rows=%s", cache_path, len(frame))
        return frame

    @classmethod
    def _read_valid_groww_instrument_master(cls, cache_path: str) -> pd.DataFrame:
        size = os.path.getsize(cache_path)
        if size < 64:
            raise RuntimeError(f"instrument master cache is too small ({size} bytes)")
        try:
            frame = pd.read_csv(cache_path, dtype=str)
        except pd.errors.EmptyDataError as exc:
            raise RuntimeError("instrument master cache is empty") from exc
        return cls._validate_groww_instrument_frame(frame)

    @staticmethod
    def _validate_groww_instrument_frame(frame: pd.DataFrame) -> pd.DataFrame:
        required = {"underlying_symbol", "segment", "expiry_date"}
        columns = {str(col).strip().lower() for col in frame.columns}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(f"Groww instrument master missing required columns: {', '.join(missing)}")
        if frame.empty:
            raise RuntimeError("Groww instrument master contains no rows")
        usable = frame.dropna(subset=[next(col for col in frame.columns if str(col).strip().lower() == "underlying_symbol")])
        if usable.empty:
            raise RuntimeError("Groww instrument master contains no usable instrument rows")
        return frame
    def _groww_trading_symbol(self, symbol: str) -> str:
        configured = str(getattr(self._dhan, "groww_trading_symbol", "") or "").strip().upper()
        if configured:
            return configured
        raw = normalize_market_symbol(symbol)
        exchange = str(self._groww.groww_exchange or "NSE").strip().upper()
        if raw.startswith(f"{exchange}-"):
            return raw[len(exchange) + 1 :]
        return raw

    @staticmethod
    def _groww_index_context(symbol: str) -> dict[str, Any]:
        return GROWW_INDEX_MAP.get(normalize_market_symbol(symbol), {})

    def _groww_underlying_symbol(self, symbol: str) -> str:
        context = self._groww_index_context(symbol)
        if context:
            return context["underlying"]
        configured = str(getattr(self._dhan, "groww_underlying_symbol", "") or "").strip().upper()
        if configured:
            return configured
        return self._groww_trading_symbol(symbol)

    def _parse_groww_contract(self, payload: dict[str, Any], *, strike: float, option_type: str) -> OptionContract | None:
        trading_symbol = payload.get("trading_symbol") or payload.get("tradingSymbol")
        if not trading_symbol:
            return None
        greeks = payload.get("greeks") if isinstance(payload.get("greeks"), dict) else {}
        buy_depth = payload.get("depth", {}).get("buy") if isinstance(payload.get("depth"), dict) else None
        sell_depth = payload.get("depth", {}).get("sell") if isinstance(payload.get("depth"), dict) else None
        bid_row = buy_depth[0] if isinstance(buy_depth, list) and buy_depth and isinstance(buy_depth[0], dict) else {}
        ask_row = sell_depth[0] if isinstance(sell_depth, list) and sell_depth and isinstance(sell_depth[0], dict) else {}
        return OptionContract(
            security_id=str(trading_symbol),
            strike=float(strike),
            option_type=option_type,
            ltp=self._to_float(payload.get("ltp") or payload.get("last_price") or payload.get("lastPrice")),
            oi=self._to_float(payload.get("open_interest") or payload.get("openInterest") or payload.get("oi")),
            oi_change=self._to_float(payload.get("oi_day_change") or payload.get("oiDayChange") or payload.get("oi_change")),
            volume=self._to_float(payload.get("volume")),
            delta=self._optional_float(greeks.get("delta") or payload.get("delta")),
            iv=self._optional_float(
                greeks.get("iv") or greeks.get("implied_volatility")
                or payload.get("iv") or payload.get("implied_volatility") or payload.get("impliedVolatility")
            ),
            theta=self._optional_float(greeks.get("theta") or payload.get("theta")),
            bid_price=self._optional_float(payload.get("bid_price") or bid_row.get("price")),
            ask_price=self._optional_float(payload.get("offer_price") or payload.get("ask_price") or ask_row.get("price")),
            bid_qty=self._optional_float(payload.get("bid_quantity") or bid_row.get("quantity")),
            ask_qty=self._optional_float(payload.get("offer_quantity") or payload.get("ask_quantity") or ask_row.get("quantity")),
            vega=self._optional_float(greeks.get("vega") or payload.get("vega")),
            gamma=self._optional_float(greeks.get("gamma") or payload.get("gamma")),
        )

    @staticmethod
    def _parse_groww_timestamp(value: Any) -> pd.Timestamp:
        if isinstance(value, (int, float)):
            unit = "ms" if value > 9_999_999_999 else "s"
            return pd.to_datetime(value, unit=unit, utc=True, errors="coerce")
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return parsed
        timestamp = pd.Timestamp(parsed)
        if timestamp.tzinfo is None:
            # Groww documents string candle timestamps in exchange-local time.
            return timestamp.tz_localize("Asia/Kolkata").tz_convert("UTC")
        return timestamp.tz_convert("UTC")

    def _resolve_equity_context(self, symbol: str) -> tuple[str, str, str]:
        symbol_key = normalize_market_symbol(symbol)

        registry = SYMBOL_CONFIG.get(symbol_key)
        if registry and registry.get("instrument_type") == "stock":
            if self._settings is None:
                raise RuntimeError(f"Groww cash instrument not found for symbol: {symbol_key}")
            security_id = self._settings.nifty50_security_map.get(symbol_key) or self._dhan.nifty_security_id
            return security_id, self._dhan.equity_exchange_segment, self._dhan.equity_instrument

        index_symbol = normalize_market_symbol(self._settings.nifty_index_symbol if self._settings else "NIFTY 50")
        if symbol_key == index_symbol:
            return (
                self._dhan.nifty_security_id,
                self._dhan.nifty_exchange_segment,
                self._dhan.nifty_instrument,
            )

        if self._settings is None:
            raise RuntimeError(
                f"Security ID mapping missing for symbol {symbol_key}. "
                "Use full Settings context with NIFTY50_SECURITY_MAP configured."
            )

        security_id = self._settings.nifty50_security_map.get(symbol_key)
        if not security_id:
            raise RuntimeError(f"Security ID is not configured for symbol {symbol_key}.")

        return (
            security_id,
            self._dhan.nifty_exchange_segment,
            self._dhan.nifty_instrument,
        )

    def _resolve_option_chain_context(self, symbol: str) -> tuple[str, str]:
        security_id, exchange_segment, _ = self._resolve_equity_context(symbol)
        return security_id, exchange_segment

    def _resolve_symbol_context(self, symbol: str) -> tuple[str, str, str]:
        return self._resolve_equity_context(symbol)

    @staticmethod
    def _parse_timeframe_to_minutes(timeframe: str) -> int:
        value = timeframe.strip().lower()
        if value.endswith("m"):
            value = value[:-1]
        if not value.isdigit():
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        minutes = int(value)
        if minutes <= 0:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return minutes

    @staticmethod
    def _extract_error_message(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None

        for key in ("remarks", "message", "error", "statusMessage", "errorMessage", "detail", "description"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        payload = data.get("data")
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _can_float(value: Any) -> bool:
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    def _extract_option_leg(self, payload: dict[str, Any]) -> dict[str, Any]:
        oi_value = self._to_float(payload.get("open_interest") or payload.get("openInterest") or payload.get("oi"))
        if oi_value < 0:
            logger.warning("Negative OI received in option leg payload. Resetting to zero: %s", oi_value)
            oi_value = 0.0

        return {
            "security_id": str(payload.get("security_id") or payload.get("securityId") or ""),
            "ltp": round(self._to_float(payload.get("last_price") or payload.get("lastPrice") or payload.get("ltp")), 2),
            "oi": oi_value,
            "oi_change": self._to_float(
                payload.get("oi_change")
                or payload.get("change_in_oi")
                or payload.get("changeInOI")
                or payload.get("oiChange")
            ),
            "volume": self._to_float(payload.get("volume") or payload.get("traded_volume") or payload.get("tradedVolume")),
        }




















