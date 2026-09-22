from __future__ import annotations

import logging
import csv
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from threading import Lock, Thread
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from ..config import SYMBOL_CONFIG, SUPPORTED_MARKET_SYMBOLS, Settings, get_settings, normalize_market_symbol
from ..db import SessionLocal
from ..models import AlertEvent, MarketCandle, OptionOISnapshot, RefreshRun, S9BestStrikeResult, S9FilterResult, S9TopOpportunitySnapshot, ScreenerResult
from ..utils.time import ist_now_naive
from ..utils.indicators import build_indicator_frame, latest_indicator_point, previous_indicator_point
from ..utils.timeframe import resample_ohlcv, take_lookback
from .alert_engine import AlertEngine
from .auto_trade_service import AutoTradeService
from .cache_service import CacheService
from .data_ingestion_service import DataIngestionService
from .indicator_engine import IndicatorEngine
from .indicator_engine import SymbolIndicatorState
from .macro_context_service import MacroContextService
from .option_metric_service import OptionDeltaCacheProducer
from .option_types import OptionChainSnapshot
from .screener_engine import ScreenerEngine, ScreenerSignal
from .s9_override_service import S9OverrideService

logger = logging.getLogger(__name__)
import time as timer


class RefreshService:
    def __init__(self, settings: Settings | None = None, *, snapshot_broadcaster: Any | None = None):
        self._settings = settings or get_settings()

        self._cache = CacheService(self._settings)
        self._ingestion = DataIngestionService(self._settings)
        self._indicator_engine = IndicatorEngine(self._settings)
        self._macro_context = MacroContextService(self._settings)
        self._option_delta_producer = OptionDeltaCacheProducer(self._settings, self._cache)
        self._screener_engine = ScreenerEngine(self._settings)
        self._snapshot_broadcaster = snapshot_broadcaster
        self._s9_override = S9OverrideService()
        self._alert_engine = AlertEngine(self._settings)
        self._auto_trade = AutoTradeService(self._settings)

        self._run_lock = Lock()
        self._dedupe_lock = Lock()
        self._s9_top_refresh_lock = Lock()
        self._s9_top_cache_write_lock = Lock()
        self._s9_top_refresh_in_progress = False
        self._s9_top_ltp_refresh_in_progress = False
        self._s9_top_last_full_refresh_at: datetime | None = None
        self._s9_top_next_auto_refresh_at: datetime | None = None
        self._recent_refresh_requests: dict[str, float] = {}
        self._market_tz = ZoneInfo(self._settings.market_timezone)
        self._market_open = self._parse_hhmm(self._settings.market_open_time)
        self._market_close = self._parse_hhmm(self._settings.market_close_time)

    def run_refresh(
        self,
        *,
        trigger: str = "manual",
        force: bool = False,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if dedupe_key and not self._claim_refresh_request(dedupe_key):
            logger.info("Refresh request debounced trigger=%s key=%s", trigger, dedupe_key)
            return {
                "status": "debounced",
                "reason": "duplicate_override_refresh",
                "trigger": trigger,
                "dedupe_key": dedupe_key,
            }
        # lock checking added 
        print(
            "TRIGGER:",
            trigger
        )
        print("\n==============================")
        print("API REQUEST RECEIVED")
        print("TIME:", datetime.utcnow())
        print("==============================\n")
        lock_start = timer.perf_counter()
        print("WAITING FOR LOCK...")
        with self._run_lock:
            print(
                "LOCK WAIT TOOK:",
                round(timer.perf_counter() - lock_start, 2),
                "seconds"
            )

            print("LOCK ACQUIRED")
            now_market = datetime.now(self._market_tz)

            if not self.is_market_open(now_market):
                return {
                    "status": "skipped",
                    "reason": "market_closed",
                    "market_time": now_market.isoformat(),
                    "window": {
                        "open": self._settings.market_open_time,
                        "close": self._settings.market_close_time,
                        "timezone": self._settings.market_timezone,
                    },
                    "db_commit": False,
                }

            with SessionLocal() as db:
                run = RefreshRun(trigger=trigger, status="running", started_at=ist_now_naive())
                db.add(run)
                db.flush()

                try:
                    print("STEP 1: ingestion started")
                    start = timer.perf_counter()
                    ingestion_result = self._ingestion.ingest(db=db, run_id=run.id, now_market=now_market)
                    print("STEP 2: ingestion completed")
                    print(
                        "INGESTION TOOK:",
                        round(
                            timer.perf_counter() - start,
                            2
                        ),
                        "seconds"
                    )
                    print("STEP 3: indicators started")
                    # S9 consumes option delta during screening, so populate its
                    # canonical cache keys immediately after option ingestion.
                    delta_cache_summary = self._option_delta_producer.populate(
                        ingestion_result.option_chain,
                        timeframe="5m",
                    )
                    chain = ingestion_result.option_chain
                    logger.info(
                        "market_data_mode=%s provider=%s symbol=%s requested_expiry=%s resolved_expiry=%s "
                        "broker_contract_count=%s broker_delta_count=%s calculated_delta_count=%s fallback_used=%s",
                        self._settings.market_data_mode, self._settings.dhan.provider,
                        normalize_market_symbol(self._settings.underlying_symbol),
                        chain.requested_expiry.isoformat() if chain and chain.requested_expiry else None,
                        chain.expiry_date.isoformat() if chain else None,
                        len(chain.contracts) if chain else 0, delta_cache_summary["provider"],
                        delta_cache_summary["calculated"], str(bool(chain and chain.fallback_used)).lower(),
                    )
                    start = timer.perf_counter()
                    indicator_result = self._indicator_engine.compute_and_store(
                        db=db,
                        run_id=run.id,
                        frames_by_symbol=ingestion_result.frames_by_symbol,
                        now_market=now_market,
                    )
                    logger.info(
                        "INDICATOR_BUILD symbols=%s timeframes=%s duration_seconds=%.3f",
                        len(indicator_result.states_by_symbol),
                        len(indicator_result.states_by_timeframe),
                        timer.perf_counter() - start,
                    )
                    option_states, option_rsi_states = self._build_option_indicator_states(
                        db=db,
                        option_chain=ingestion_result.option_chain,
                        now_market=now_market,
                    )
                    print("STEP 3: indicators completed")
                    print(
                        "INDICATORS TOOK:",
                        round(timer.perf_counter() - start, 2),
                        "seconds"
                    )
                    macro_context = self._macro_context.get_context(now=now_market)
                    screener_result = self._screener_engine.run_all(
                        indicators=indicator_result,
                        option_chain=ingestion_result.option_chain,
                        previous_option_map=ingestion_result.previous_option_map,
                        now_market=now_market,
                        s9_override=self._s9_override.get_current(db, now=now_market.replace(tzinfo=None)),
                        option_states=option_states,
                        option_rsi_states=option_rsi_states,
                        macro_context=macro_context,
                    )
                    print(
                        "SCREENER TOOK:",
                        round(timer.perf_counter() - start, 2),
                        "seconds"
                    )

                    self._delete_stale_s1_rows(db=db)
                    self._store_screener_rows(db=db, run_id=run.id, rows=screener_result)

                    auto_trade_summary: dict[str, Any]
                    try:
                        auto_trade_summary = self._auto_trade.process(
                            db=db,
                            run_id=run.id,
                            signals=screener_result.get("S9", []),
                            option_chain=ingestion_result.option_chain,
                            states_by_timeframe=indicator_result.states_by_timeframe,
                            now_market=now_market,
                        )
                    except Exception as auto_trade_exc:  # noqa: BLE001
                        logger.exception("Auto-trade management failed without stopping the market scan")
                        auto_trade_summary = {
                            "enabled": bool(self._settings.auto_entry_enabled),
                            "dry_run": bool(self._settings.auto_entry_dry_run),
                            "error": str(auto_trade_exc),
                        }

                    alerts = self._alert_engine.build_alerts(screener_result)
                    self._store_alert_rows(db=db, run_id=run.id, rows=alerts)

                    run.status = "completed"
                    run.completed_at = ist_now_naive()
                    # data base commit timing added
                    start = timer.perf_counter()
                    db.commit()
                    print("DB COMMIT DONE")
                    print(
                        "DB COMMIT TOOK:",
                        round(timer.perf_counter() - start, 2),
                        "seconds"
                    )
                    response = {
                        "status": "completed",
                        "run_id": run.id,
                        "trigger": trigger,
                        "market_time": now_market.isoformat(),
                        "requested_expiry": (
                            chain.requested_expiry.isoformat()
                            if chain and chain.requested_expiry
                            else None
                        ),
                        "resolved_expiry": chain.expiry_date.isoformat() if chain else None,
                        "expiry_fallback_used": bool(chain and chain.fallback_used),
                        "fetched_symbols": len(ingestion_result.fetched_symbols),
                        "failed_symbols": ingestion_result.failed_symbols,
                        "screeners": {code.lower(): len(items) for code, items in screener_result.items()},
                        "alerts": len(alerts),
                        "auto_trade": auto_trade_summary,
                        "warnings": ingestion_result.warnings,
                        "delta_cache": delta_cache_summary,
                        "candle_writes": ingestion_result.candle_writes,
                    }
                    # cache refresh timing added 
                    print("CACHE REFRESH START")

                    start = timer.perf_counter()

                    self._refresh_cache(db_payload=response, run_id=run.id)
                    self._broadcast_s9_snapshot(run_id=run.id)
                    print(
                        "CACHE REFRESH TOOK:",
                        round(
                            timer.perf_counter() - start,
                            2
                        ),
                        "seconds"
                    )
                    return response
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Refresh pipeline failed")
                    db.rollback()
                    try:
                        failed_run = db.get(RefreshRun, run.id) if run.id is not None else None
                        if failed_run is None:
                            failed_run = RefreshRun(
                                trigger=trigger, status="failed", started_at=run.started_at,
                            )
                            db.add(failed_run)
                        failed_run.status = "failed"
                        failed_run.error_message = str(exc)
                        failed_run.completed_at = ist_now_naive()
                        db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        logger.exception("Failed to persist refresh failure after rollback recovery")
                    raise

    def _claim_refresh_request(self, dedupe_key: str, *, now: float | None = None) -> bool:
        key = str(dedupe_key or "").strip()
        if not key:
            return True
        current = timer.monotonic() if now is None else float(now)
        window = float(self._settings.s9_override_refresh_debounce_seconds)
        with self._dedupe_lock:
            previous = self._recent_refresh_requests.get(key)
            if previous is not None and current - previous < window:
                return False
            self._recent_refresh_requests[key] = current
            cutoff = current - max(window * 4.0, 30.0)
            self._recent_refresh_requests = {
                item_key: timestamp
                for item_key, timestamp in self._recent_refresh_requests.items()
                if timestamp >= cutoff
            }
        return True

    def _build_option_indicator_states(
        self,
        *,
        db: Any,
        option_chain: OptionChainSnapshot | None,
        now_market: datetime,
    ) -> tuple[dict[tuple[float, str], SymbolIndicatorState], dict[tuple[float, str], SymbolIndicatorState]]:
        if option_chain is None or not option_chain.contracts:
            return {}, {}

        market_tz = ZoneInfo(self._settings.market_timezone)
        cutoff = now_market.astimezone(market_tz).replace(tzinfo=None) - timedelta(days=max(1, int(self._settings.intraday_fetch_days)))
        contract_keys = {
            (float(contract.strike), str(contract.option_type).strip().upper())
            for contract in option_chain.contracts
        }
        strikes = sorted({strike for strike, _option_type in contract_keys})
        option_types = sorted({option_type for _strike, option_type in contract_keys})
        if not strikes or not option_types:
            return {}, {}

        rows = list(
            db.scalars(
                select(OptionOISnapshot)
                .where(
                    OptionOISnapshot.underlying == option_chain.underlying,
                    OptionOISnapshot.expiry_date == option_chain.expiry_date,
                    OptionOISnapshot.strike_price.in_(strikes),
                    OptionOISnapshot.option_type.in_(option_types),
                    OptionOISnapshot.snapshot_time >= cutoff,
                )
                .order_by(
                    OptionOISnapshot.strike_price.asc(),
                    OptionOISnapshot.option_type.asc(),
                    OptionOISnapshot.snapshot_time.asc(),
                )
            )
        )
        if not rows:
            return {}, {}

        grouped: dict[tuple[float, str], list[OptionOISnapshot]] = {}
        for row in rows:
            key = (float(row.strike_price), str(row.option_type).strip().upper())
            if key in contract_keys:
                grouped.setdefault(key, []).append(row)

        option_states: dict[tuple[float, str], SymbolIndicatorState] = {}
        option_rsi_states: dict[tuple[float, str], SymbolIndicatorState] = {}
        for key, key_rows in grouped.items():
            frame = self._option_snapshot_frame(key_rows, market_tz=market_tz)
            if frame.empty:
                continue
            symbol = self._option_state_symbol(option_chain.underlying, key)
            state_3m = self._indicator_state_from_frame(
                symbol=symbol,
                timeframe="3m",
                frame=frame,
                now_market=now_market,
            )
            if state_3m is not None:
                option_states[key] = state_3m
            state_15m = self._indicator_state_from_frame(
                symbol=symbol,
                timeframe="15m",
                frame=frame,
                now_market=now_market,
            )
            if state_15m is not None:
                option_rsi_states[key] = state_15m

        logger.info(
            "Option premium indicator states built underlying=%s expiry=%s option_states=%s option_rsi_states=%s",
            option_chain.underlying,
            option_chain.expiry_date.isoformat(),
            len(option_states),
            len(option_rsi_states),
        )
        return option_states, option_rsi_states

    @staticmethod
    def _option_state_symbol(underlying: str, key: tuple[float, str]) -> str:
        strike, option_type = key
        strike_text = str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
        return f"{normalize_market_symbol(underlying)} {strike_text} {option_type}"

    @staticmethod
    def _option_snapshot_frame(rows: list[OptionOISnapshot], *, market_tz: ZoneInfo) -> pd.DataFrame:
        payload: list[dict[str, Any]] = []
        for row in rows:
            candle_time = row.snapshot_time
            timestamp = pd.Timestamp(candle_time)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(market_tz)
            else:
                timestamp = timestamp.tz_convert(market_tz)
            ltp = float(row.ltp or 0.0)
            if ltp <= 0:
                continue
            payload.append(
                {
                    "timestamp": timestamp,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": max(0.0, float(row.traded_volume or 0.0)),
                }
            )
        if not payload:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(payload).sort_values("timestamp")
        frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
        return frame.set_index("timestamp")

    def _indicator_state_from_frame(
        self,
        *,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        now_market: datetime,
    ) -> SymbolIndicatorState | None:
        resampled = resample_ohlcv(
            frame,
            timeframe=timeframe,
            timezone_name=self._settings.market_timezone,
            market_open_time=self._settings.market_open_time,
            now_market=now_market,
        )
        lookback = take_lookback(resampled, candles=self._settings.lookback_candles)
        if lookback.empty:
            return None
        indicator_frame = build_indicator_frame(lookback)
        latest = latest_indicator_point(indicator_frame)
        previous = previous_indicator_point(indicator_frame)
        if latest is None:
            return None
        return SymbolIndicatorState(symbol=symbol, timeframe=timeframe, latest=latest, previous=previous)

    def get_screener_results(self, *, screener_code: str, limit: int = 50) -> dict[str, Any]:
        code = screener_code.upper()
        cache_key = self._screener_cache_key(code)
        cached = self._cache.get_json(cache_key)
        if cached is not None:
            if not isinstance(cached, dict):
                return cached
            items_all = cached.get("items") if isinstance(cached.get("items"), list) else []
            if not self._payload_matches_selected_index(items_all):
                self._cache.delete(cache_key)
            else:
                trimmed = items_all[: max(0, int(limit))]
                payload = {
                    **cached,
                    "items": trimmed,
                    # Preserve the untrimmed count when present so the UI can show "shown / total".
                    "count": int(cached.get("count") or len(items_all)),
                }
                return payload

        with SessionLocal() as db:
            run = self._latest_completed_run(db, screener_code=code)
            if run is None:
                payload = {
                    "screener": code,
                    "run": None,
                    "count": 0,
                    "items": [],
                }
                self._cache.set_json(cache_key, payload)
                return payload

            rows_all = list(
                db.scalars(
                    select(ScreenerResult)
                    .where(ScreenerResult.run_id == run.id, ScreenerResult.screener_code == code)
                    .order_by(ScreenerResult.confidence.desc())
                )
            )
            rows_all = [row for row in rows_all if self._is_selected_index_symbol(row.symbol)]
            rows = rows_all[: max(0, int(limit))]

            payload = {
                "screener": code,
                "run": self._serialize_run(run),
                "count": len(rows_all),
                "items": [self._serialize_screener_row(row) for row in rows],
            }
            self._cache.set_json(cache_key, payload)
            return payload

    def get_s9_top_opportunities(self, *, limit: int = 6) -> dict[str, Any]:
        started = timer.perf_counter()
        cache_key = self._s9_top_opportunities_cache_key()
        cached = self._cache.get_json(cache_key)
        if isinstance(cached, dict):
            if cached.get("cache_version") and not self._s9_top_payload_is_current_market_day(cached):
                logger.info(
                    "S9_TOP_CACHE_STALE ignored cache_version=%s cache_run_id=%s",
                    cached.get("cache_version"),
                    cached.get("cache_run_id"),
                )
                self._cache.delete(cache_key)
                self._schedule_s9_top_opportunities_refresh(limit=limit, reason="stale_market_day_read")
                payload = self._empty_s9_top_opportunities(limit=limit)
                payload["refresh_type"] = "bootstrap_pending"
                payload["refreshing"] = True
                return payload
            items = cached.get("items") if isinstance(cached.get("items"), list) else []
            ordered = self._ensure_s9_mandatory_top_items(items, limit=limit)
            payload = {
                **cached,
                "items": ordered,
                "count": max(int(cached.get("count") or len(items)), len(ordered)),
                "refreshing": bool(self._s9_top_refresh_in_progress),
            }
            logger.info(
                "S9_TOP_CACHE_HIT rows=%s refreshing=%s duration_seconds=%.4f",
                len(ordered),
                payload["refreshing"],
                timer.perf_counter() - started,
            )
            logger.info("S9_TOP_GET_DURATION duration_seconds=%.4f", timer.perf_counter() - started)
            return payload
        payload = self._empty_s9_top_opportunities(limit=limit)
        payload["refreshing"] = True
        logger.info("S9_TOP_GET_DURATION duration_seconds=%.4f", timer.perf_counter() - started)
        return payload

    def restore_s9_top_opportunities_from_db(self, *, limit: int = 6) -> dict[str, Any] | None:
        cache_key = self._s9_top_opportunities_cache_key()
        cached = self._cache.get_json(cache_key)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            return cached
        try:
            with SessionLocal() as db:
                snapshot = db.scalar(
                    select(S9TopOpportunitySnapshot)
                    .order_by(S9TopOpportunitySnapshot.created_at.desc())
                    .limit(1)
                )
        except SQLAlchemyError:
            logger.info("S9_TOP_DB_RESTORE unavailable snapshot table not initialized")
            return None
        if snapshot is None or not isinstance(snapshot.payload, dict):
            return None
        payload = dict(snapshot.payload)
        if not self._s9_top_payload_is_current_market_day(payload):
            return None
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        payload["items"] = self._ensure_s9_mandatory_top_items(items, limit=limit)
        payload["count"] = max(int(payload.get("count") or len(items)), len(payload["items"]))
        payload["refreshing"] = True
        ttl_seconds = max(int(self._settings.redis_ttl_seconds), self._s9_top_auto_refresh_interval_seconds() * 2, 30)
        self._cache.set_json(cache_key, payload, ttl_seconds=ttl_seconds)
        logger.info("S9_TOP_DB_RESTORE rows=%s cache_version=%s", len(payload["items"]), payload.get("cache_version"))
        return payload

    def schedule_s9_top_background_refresh(self, *, limit: int = 6, reason: str = "s9_get") -> None:
        self._schedule_s9_top_opportunities_refresh(limit=limit, reason=reason)

    def schedule_s9_top_ltp_refresh(self, *, limit: int = 6) -> None:
        with self._s9_top_refresh_lock:
            if self._s9_top_ltp_refresh_in_progress:
                return
            self._s9_top_ltp_refresh_in_progress = True

        def worker() -> None:
            try:
                self.refresh_s9_top_ltp_cache(limit=limit)
            except Exception:  # noqa: BLE001
                logger.exception("S9_TOP_LTP_REFRESH background failed")
            finally:
                with self._s9_top_refresh_lock:
                    self._s9_top_ltp_refresh_in_progress = False

        Thread(target=worker, name="s9-top-ltp-refresh", daemon=True).start()

    def refresh_s9_top_opportunities(self, *, limit: int = 6) -> dict[str, Any]:
        with self._run_lock:
            scan_started = timer.perf_counter()
            scan_started_at = datetime.now(self._market_tz)
            previous_payload = self.get_s9_top_opportunities(limit=limit)
            previous_items = previous_payload.get("items") if isinstance(previous_payload.get("items"), list) else []
            previous_top = self._s9_item_summaries(previous_items)
            previous_symbols = {item["symbol"] for item in previous_top if item.get("symbol")}
            first_seen_gtp = self._s9_first_seen_gtp_snapshots(previous_payload)
            with self._s9_top_refresh_lock:
                next_due_at = self._s9_top_next_auto_refresh_at

            universe = self._s9_scan_universe()
            logger.info(
                "S9_TOP_FULL_REFRESH_CYCLE_START universe_count=%s instruments=%s previous_top=%s next_due_at=%s",
                len(universe),
                universe,
                previous_top,
                self._iso_or_none(next_due_at),
            )

            items: list[dict[str, Any]] = []
            runs: dict[str, dict[str, Any] | None] = {}
            errors: dict[str, str] = {}
            refreshed_symbols: list[str] = []
            instrument_scan_details: list[dict[str, Any]] = []
            total_strikes_evaluated = 0

            for market_symbol in universe:
                symbol_key = normalize_market_symbol(market_symbol)
                symbol_started = timer.perf_counter()
                try:
                    service = self._s9_service_for_symbol(symbol_key)
                    refresh_result = service.run_refresh(trigger="s9_top_opportunities", force=True)
                    if isinstance(refresh_result, dict) and refresh_result.get("status") == "completed":
                        refreshed_symbols.append(symbol_key)

                    payload = service.get_screener_results(screener_code="S9", limit=50)
                    runs[symbol_key] = payload.get("run") if isinstance(payload, dict) else None
                    symbol_items: list[dict[str, Any]] = []
                    for item in payload.get("items", []) if isinstance(payload, dict) else []:
                        if isinstance(item, dict):
                            symbol_items.append(item)
                            items.append(item)
                    strikes_evaluated = sum(self._s9_strikes_evaluated(item) for item in symbol_items)
                    total_strikes_evaluated += strikes_evaluated
                    detail = {
                        "symbol": symbol_key,
                        "items": len(symbol_items),
                        "strikes_evaluated": strikes_evaluated,
                        "duration_seconds": round(timer.perf_counter() - symbol_started, 3),
                    }
                    instrument_scan_details.append(detail)
                    logger.info(
                        "S9_TOP_INSTRUMENT_SCAN symbol=%s items=%s strikes_evaluated=%s duration_seconds=%.3f",
                        symbol_key,
                        detail["items"],
                        detail["strikes_evaluated"],
                        detail["duration_seconds"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("S9 top opportunity scan failed symbol=%s", symbol_key)
                    runs[symbol_key] = None
                    errors[symbol_key] = str(exc)
                    instrument_scan_details.append(
                        {
                            "symbol": symbol_key,
                            "items": 0,
                            "strikes_evaluated": 0,
                            "duration_seconds": round(timer.perf_counter() - symbol_started, 3),
                            "error": str(exc),
                        }
                    )

            ordered, diagnostics = self._rank_s9_top_opportunities(items, limit=limit)
            logger.info(
                "S9_TOP_FULL_SCAN_CANDIDATES candidate_count=%s ranked_count=%s errors=%s",
                diagnostics["candidate_count"],
                len(ordered),
                sorted(errors),
            )
            first_seen_gtp = self._apply_s9_historical_gtp_snapshots(
                ordered,
                first_seen_gtp=first_seen_gtp,
            )
            new_top = self._s9_item_summaries(ordered)
            new_symbols = {item["symbol"] for item in new_top if item.get("symbol")}
            replacements = {
                "added": sorted(new_symbols - previous_symbols),
                "removed": sorted(previous_symbols - new_symbols),
            }
            scan_duration_seconds = round(timer.perf_counter() - scan_started, 3)
            cache_version = datetime.utcnow().isoformat()
            run_ids = [
                int(run_payload["id"])
                for run_payload in runs.values()
                if isinstance(run_payload, dict) and run_payload.get("id") is not None
            ]
            cache_run = max(run_ids) if run_ids else None
            scan_completed_at = datetime.now(self._market_tz)
            with self._s9_top_refresh_lock:
                # Direct/manual full refreshes also establish the next ranking
                # deadline. Scheduled/background callers reserve it before the
                # scan starts and retain that configured cycle boundary.
                if not self._s9_top_refresh_in_progress:
                    self._s9_top_last_full_refresh_at = scan_completed_at
                    self._s9_top_next_auto_refresh_at = scan_completed_at + timedelta(
                        seconds=self._s9_top_auto_refresh_interval_seconds()
                    )
                next_due_at = self._s9_top_next_auto_refresh_at
            payload = {
                "screener": "S9",
                "scope": "market_universe",
                "refresh_type": "ranking",
                "cache_version": cache_version,
                "cache_run_id": cache_run,
                "universe": universe,
                "count": len(ordered),
                "items": ordered,
                "runs": runs,
                "errors": errors,
                "diagnostics": {
                    **diagnostics,
                    "refreshed_symbols": sorted(refreshed_symbols),
                    "previous_top": previous_top,
                    "new_top": new_top,
                    "replacements": replacements,
                    "instruments_scanned": universe,
                    "instrument_scan_details": instrument_scan_details,
                    "strikes_evaluated": total_strikes_evaluated,
                    "scan_duration_seconds": scan_duration_seconds,
                    "cache_version": cache_version,
                    "cache_run_id": cache_run,
                    "scan_started_at": scan_started_at.isoformat(),
                    "scan_completed_at": scan_completed_at.isoformat(),
                    "next_ranking_refresh_at": self._iso_or_none(next_due_at),
                },
                "gtp_first_seen": first_seen_gtp,
                "ranking_refresh": {
                    "status": "completed",
                    "updated_at": scan_completed_at.isoformat(),
                    "next_refresh_at": self._iso_or_none(next_due_at),
                },
            }

            ttl_seconds = max(
                int(self._settings.redis_ttl_seconds),
                self._s9_top_auto_refresh_interval_seconds() * 2,
                30,
            )
            cache_replace_started = timer.perf_counter()
            cache_replaced_at = datetime.now(self._market_tz)
            payload["cache_replaced_at"] = cache_replaced_at.isoformat()
            with self._s9_top_cache_write_lock:
                self._cache.set_json(self._s9_top_opportunities_cache_key(), payload, ttl_seconds=ttl_seconds)
            try:
                with SessionLocal() as db:
                    db.add(
                        S9TopOpportunitySnapshot(
                            cache_version=cache_version,
                            payload=payload,
                        )
                    )
                    db.commit()
            except SQLAlchemyError:
                logger.warning("S9_TOP_DB_SNAPSHOT_WRITE failed cache_version=%s", cache_version, exc_info=True)
            cache_replace_seconds = round(timer.perf_counter() - cache_replace_started, 6)
            logger.info(
                "S9_TOP_CACHE_REPLACED replaced_at=%s cache_replace_seconds=%.6f "
                "candidate_count=%s old_top_6=%s new_top_6=%s cache_run=%s cache_version=%s "
                "next_ranking_refresh_at=%s",
                cache_replaced_at.isoformat(),
                cache_replace_seconds,
                diagnostics["candidate_count"],
                previous_top,
                new_top,
                cache_run,
                cache_version,
                self._iso_or_none(next_due_at),
            )
            logger.info(
                "S9_TOP_FULL_REFRESH_CYCLE_DONE instruments_scanned=%s strikes_evaluated=%s "
                "candidate_count=%s previous_top=%s new_top=%s replacements=%s "
                "scan_duration_seconds=%.3f cache_run=%s cache_version=%s next_due_at=%s",
                universe,
                total_strikes_evaluated,
                diagnostics["candidate_count"],
                previous_top,
                new_top,
                replacements,
                scan_duration_seconds,
                cache_run,
                cache_version,
                self._iso_or_none(next_due_at),
            )
            return {
                **payload,
                "items": ordered[: max(0, int(limit))],
                "count": len(ordered),
            }

    def refresh_s9_top_ltp_cache(self, *, limit: int = 6) -> dict[str, Any]:
        cache_key = self._s9_top_opportunities_cache_key()
        cached = self._cache.get_json(cache_key)
        if not isinstance(cached, dict) or not isinstance(cached.get("items"), list):
            self._schedule_s9_top_opportunities_refresh(
                limit=limit,
                reason="missing_top_opportunities_cache",
            )
            payload = self._empty_s9_top_opportunities(limit=limit)
            payload["refresh_type"] = "bootstrap_pending"
            payload["refreshing"] = True
            payload["ltp_refresh"] = {
                "status": "bootstrap_scheduled",
                "updated_at": datetime.utcnow().isoformat(),
                "ranking_unchanged": True,
            }
            return payload
        if not cached.get("cache_version"):
            payload = self._empty_s9_top_opportunities(limit=limit)
            payload["refresh_type"] = "bootstrap_pending"
            payload["refreshing"] = True
            payload["ltp_refresh"] = {
                "status": "no_cache",
                "updated_at": datetime.utcnow().isoformat(),
                "ranking_unchanged": True,
            }
            return payload
        if not self._s9_top_payload_is_current_market_day(cached):
            logger.info(
                "S9_TOP_LTP_REFRESH schedule_full_refresh reason=stale_market_day cache_version=%s cache_run_id=%s",
                cached.get("cache_version"),
                cached.get("cache_run_id"),
            )
            self._cache.delete(cache_key)
            payload = self._empty_s9_top_opportunities(limit=limit)
            payload["refresh_type"] = "bootstrap_pending"
            payload["refreshing"] = True
            payload["ltp_refresh"] = {
                "status": "bootstrap_scheduled",
                "updated_at": datetime.utcnow().isoformat(),
                "ranking_unchanged": True,
            }
            payload["ranking_refresh"] = {
                "status": "scheduled",
                "reason": "stale_market_day",
            }
            return payload

        source_cache_version = cached.get("cache_version")
        ranking_refresh = self._schedule_s9_top_opportunities_refresh_if_due(
            limit=limit,
            reason="ltp_poll_ranking_due",
        )

        payload = deepcopy(cached)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        symbols = {
            self._s9_item_symbol(item)
            for item in items
            if isinstance(item, dict)
        }
        symbols.discard("")
        updated_at = datetime.utcnow().isoformat()
        updated_count = 0
        errors: dict[str, str] = {}

        def fetch_symbol_ltp(symbol_key: str) -> tuple[str, dict[tuple[float, str], float]]:
            service = self._s9_service_for_symbol(symbol_key)
            chain = service._ingestion.fetch_option_chain_snapshot()  # noqa: SLF001
            return symbol_key, {
                (float(contract.strike), str(contract.option_type).upper()): float(contract.ltp)
                for contract in chain.contracts
            }

        # Top rows can belong to several instruments. Fetch their option
        # chains concurrently so the LTP-only cycle can comfortably keep its
        # 10-second UI cadence without turning into a full ranking scan.
        workers = max(1, min(len(symbols), 6))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s9-ltp") as executor:
            futures = {
                executor.submit(fetch_symbol_ltp, symbol_key): symbol_key
                for symbol_key in sorted(symbols)
            }
            for future in as_completed(futures):
                symbol_key = futures[future]
                try:
                    _, ltp_by_contract = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("S9 top LTP-only refresh failed symbol=%s error=%s", symbol_key, exc)
                    errors[symbol_key] = str(exc)
                    continue

                for item in items:
                    if not isinstance(item, dict) or self._s9_item_symbol(item) != symbol_key:
                        continue
                    row_payload = item.get("payload")
                    if isinstance(row_payload, dict):
                        updated_count += self._update_s9_payload_ltp(
                            row_payload,
                            ltp_by_contract=ltp_by_contract,
                            updated_at=updated_at,
                        )

        ttl_seconds = max(
            int(self._settings.redis_ttl_seconds),
            self._s9_top_auto_refresh_interval_seconds() * 2,
            30,
        )
        ordered = self._ensure_s9_mandatory_items_preserve_order(items, limit=limit)
        payload["items"] = ordered
        payload["count"] = max(int(payload.get("count") or len(items)), len(ordered))
        payload["refresh_type"] = payload.get("refresh_type") or "ranking"
        payload["ltp_refresh"] = {
            "status": "completed" if not errors else "partial",
            "updated_at": updated_at,
            "updated_count": updated_count,
            "errors": errors,
            "ranking_unchanged": True,
        }
        payload["ranking_refresh"] = ranking_refresh
        with self._s9_top_cache_write_lock:
            current_cached = self._cache.get_json(cache_key)
            current_version = current_cached.get("cache_version") if isinstance(current_cached, dict) else None
            if current_version and current_version != source_cache_version:
                # A full scan completed while this slower LTP request was in
                # flight. Never put the old ranking back over the new Top-6.
                latest = deepcopy(current_cached)
                latest_items = latest.get("items") if isinstance(latest.get("items"), list) else []
                latest["ltp_refresh"] = {
                    "status": "superseded_by_full_scan",
                    "updated_at": updated_at,
                    "updated_count": 0,
                    "errors": errors,
                    "ranking_unchanged": False,
                }
                latest["ranking_refresh"] = ranking_refresh
                logger.info(
                    "S9_TOP_LTP_REFRESH discarded_stale_write source_cache_version=%s "
                    "current_cache_version=%s ranking_refresh_status=%s next_refresh_at=%s",
                    source_cache_version,
                    current_version,
                    ranking_refresh.get("status"),
                    ranking_refresh.get("next_refresh_at"),
                )
                return {
                    **latest,
                    "items": latest_items[: max(0, int(limit))],
                    "count": int(latest.get("count") or len(latest_items)),
                }
            self._cache.set_json(cache_key, payload, ttl_seconds=ttl_seconds)
        logger.info(
            "S9_TOP_LTP_REFRESH updated_count=%s symbols=%s cache_run=%s cache_version=%s "
            "ranking_unchanged=true ranking_refresh_status=%s reason=%s",
            updated_count,
            sorted(symbols),
            payload.get("cache_run_id"),
            payload.get("cache_version"),
            ranking_refresh.get("status"),
            ranking_refresh.get("reason"),
        )
        return {
            **payload,
            "items": ordered,
            "count": int(payload.get("count") or len(ordered)),
        }

    def refresh_s9_top_opportunities_if_due(
        self,
        *,
        now_market: datetime | None = None,
        force: bool = False,
        limit: int = 6,
    ) -> dict[str, Any]:
        now = now_market or datetime.now(self._market_tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._market_tz)
        interval = timedelta(seconds=self._s9_top_auto_refresh_interval_seconds())
        with self._s9_top_refresh_lock:
            next_refresh_at = self._s9_top_next_auto_refresh_at
            if not force and next_refresh_at is not None and now < next_refresh_at:
                return {
                    "status": "skipped",
                    "reason": "not_due",
                    "next_refresh_at": next_refresh_at.isoformat(),
                }
            if self._s9_top_refresh_in_progress:
                logger.info(
                    "S9_TOP_AUTO_REFRESH skipped reason=already_running next_refresh_at=%s",
                    next_refresh_at.isoformat() if next_refresh_at is not None else None,
                )
                return {
                    "status": "skipped",
                    "reason": "already_running",
                    "next_refresh_at": next_refresh_at.isoformat() if next_refresh_at is not None else None,
                }
            self._s9_top_refresh_in_progress = True
            last_full_refresh_at = self._s9_top_last_full_refresh_at
            self._s9_top_next_auto_refresh_at = now + interval
            next_refresh_at = self._s9_top_next_auto_refresh_at

        cache_identity = self._s9_top_cache_identity()
        logger.info(
            "S9_TOP_AUTO_REFRESH started service_id=%s last_full_refresh_at=%s next_due_at=%s "
            "cache_version=%s cache_run_id=%s",
            id(self),
            self._iso_or_none(last_full_refresh_at),
            next_refresh_at.isoformat(),
            cache_identity["cache_version"],
            cache_identity["cache_run_id"],
        )
        try:
            payload = self.refresh_s9_top_opportunities(limit=limit)
            final_top = self._s9_item_summaries(payload.get("items") if isinstance(payload, dict) else [])
            with self._s9_top_refresh_lock:
                self._s9_top_last_full_refresh_at = now
                self._s9_top_next_auto_refresh_at = now + interval
                next_refresh_at = self._s9_top_next_auto_refresh_at
            logger.info(
                "S9_TOP_AUTO_REFRESH completed service_id=%s cache_version=%s cache_run_id=%s "
                "last_full_refresh_at=%s next_due_at=%s final_top_6=%s",
                id(self),
                payload.get("cache_version") if isinstance(payload, dict) else None,
                payload.get("cache_run_id") if isinstance(payload, dict) else None,
                now.isoformat(),
                next_refresh_at.isoformat(),
                final_top,
            )
            return {
                "status": "completed",
                "next_refresh_at": next_refresh_at.isoformat(),
                "cache_version": payload.get("cache_version") if isinstance(payload, dict) else None,
                "final_top": final_top,
                "payload": payload,
            }
        except Exception:
            with self._s9_top_refresh_lock:
                # A failed refresh must be eligible for retry on the next scheduler tick.
                self._s9_top_next_auto_refresh_at = now
            logger.exception(
                "S9_TOP_AUTO_REFRESH failed service_id=%s retry_due_at=%s",
                id(self),
                now.isoformat(),
            )
            raise
        finally:
            with self._s9_top_refresh_lock:
                self._s9_top_refresh_in_progress = False

    def s9_top_refresh_schedule_state(self, *, now_market: datetime | None = None) -> dict[str, Any]:
        """Return a read-only snapshot used by scheduler runtime diagnostics."""
        now = now_market or datetime.now(self._market_tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._market_tz)
        with self._s9_top_refresh_lock:
            last_full_refresh_at = self._s9_top_last_full_refresh_at
            next_due_at = self._s9_top_next_auto_refresh_at
            in_progress = self._s9_top_refresh_in_progress
        return {
            "current_time": now.isoformat(),
            "last_full_refresh_at": self._iso_or_none(last_full_refresh_at),
            "next_due_at": self._iso_or_none(next_due_at),
            "due": next_due_at is None or now >= next_due_at,
            "in_progress": in_progress,
        }

    def _s9_top_cache_identity(self) -> dict[str, Any]:
        cached = self._cache.get_json(self._s9_top_opportunities_cache_key())
        if not isinstance(cached, dict):
            return {"cache_version": None, "cache_run_id": None}
        return {
            "cache_version": cached.get("cache_version"),
            "cache_run_id": cached.get("cache_run_id"),
        }

    def _s9_top_payload_is_current_market_day(self, payload: dict[str, Any]) -> bool:
        today = datetime.now(self._market_tz).date()
        market_dates: list[date] = []
        runs = payload.get("runs") if isinstance(payload.get("runs"), dict) else {}
        for run_payload in runs.values():
            if not isinstance(run_payload, dict):
                continue
            for key in ("completed_at", "started_at"):
                market_date = self._market_date_from_payload_time(run_payload.get(key))
                if market_date is not None:
                    market_dates.append(market_date)
                    break
        if market_dates:
            return any(market_date == today for market_date in market_dates)

        cache_market_date = self._market_date_from_payload_time(payload.get("cache_version"))
        return cache_market_date == today if cache_market_date is not None else False

    def _market_date_from_payload_time(self, value: Any) -> date | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(self._market_tz).date()
        return parsed.date()

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @classmethod
    def _s9_first_seen_gtp_snapshots(cls, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw_map = payload.get("gtp_first_seen") if isinstance(payload, dict) else None
        first_seen: dict[str, dict[str, Any]] = {}
        if isinstance(raw_map, dict):
            for key, value in raw_map.items():
                if not key or not value:
                    continue
                if isinstance(value, dict):
                    snapshot = {
                        snapshot_key: value.get(snapshot_key)
                        for snapshot_key in ("gtp", "gtp_time", "gtp_pass", "gtp_pass_count", "gtp_total_filters")
                        if value.get(snapshot_key) is not None
                    }
                    if snapshot:
                        first_seen[str(key)] = snapshot
                else:
                    # Backward compatibility for older cache payloads that only
                    # stored gtp_time as a plain string.
                    first_seen[str(key)] = {"gtp_time": str(value)}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_key = cls._s9_gtp_identity(item)
            snapshot = cls._s9_gtp_snapshot(item)
            if item_key and snapshot and item_key not in first_seen:
                first_seen[item_key] = snapshot
        return first_seen

    @classmethod
    def _apply_s9_historical_gtp_snapshots(
        cls,
        items: list[dict[str, Any]],
        *,
        first_seen_gtp: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        for item in items:
            if not isinstance(item, dict):
                continue
            item_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            item_key = cls._s9_gtp_identity(item)
            current_snapshot = cls._s9_gtp_snapshot(item)
            if not item_key or not current_snapshot:
                continue
            historical_snapshot = first_seen_gtp.setdefault(item_key, current_snapshot)
            for snapshot_key in ("gtp", "gtp_time", "gtp_pass", "gtp_pass_count", "gtp_total_filters"):
                if snapshot_key in historical_snapshot:
                    item_payload[snapshot_key] = historical_snapshot[snapshot_key]
        return first_seen_gtp

    @staticmethod
    def _s9_gtp_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        item_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        gtp_time = item_payload.get("gtp_time")
        if not gtp_time:
            return {}
        gtp_pass_count = (
            item_payload.get("gtp_pass_count")
            if item_payload.get("gtp_pass_count") is not None
            else item_payload.get("gtp_passed_count")
        )
        if gtp_pass_count is None:
            gtp_pass_count = item_payload.get("passed_count")
        gtp_total_filters = item_payload.get("gtp_total_filters")
        if gtp_total_filters is None:
            gtp_total_filters = item_payload.get("total_filters")
        gtp_pass = item_payload.get("gtp_pass")
        if gtp_pass is None and gtp_pass_count is not None and gtp_total_filters is not None:
            gtp_pass = f"{gtp_pass_count}/{gtp_total_filters}"
        snapshot = {
            "gtp": item_payload.get("gtp"),
            "gtp_time": str(gtp_time),
            "gtp_pass": gtp_pass,
            "gtp_pass_count": gtp_pass_count,
            "gtp_total_filters": gtp_total_filters,
        }
        return {key: value for key, value in snapshot.items() if value is not None}

    @classmethod
    def _s9_gtp_identity(cls, item: dict[str, Any]) -> str | None:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        symbol = cls._s9_item_symbol(item)
        strike = payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike") or payload.get("evaluated_strike")
        option_type = (
            payload.get("selected_option_type")
            or payload.get("final_option_type")
            or payload.get("option_type")
            or payload.get("evaluated_option_type")
        )
        strike_scan = payload.get("strike_scan") if isinstance(payload.get("strike_scan"), dict) else {}
        expiry = strike_scan.get("expiry") or payload.get("expiry")
        if not symbol or strike is None or not option_type or not expiry:
            return None
        try:
            strike_key = str(int(float(strike)))
        except (TypeError, ValueError):
            strike_key = str(strike)
        return "|".join([symbol, str(expiry), strike_key, str(option_type).upper()])

    @staticmethod
    def _s9_strikes_evaluated(item: dict[str, Any]) -> int:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        scanned_contract_count = payload.get("scanned_contract_count")
        try:
            count = int(scanned_contract_count)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            return count
        scanned = payload.get("scanned_strikes") if isinstance(payload.get("scanned_strikes"), list) else []
        return len(scanned)

    def _s9_top_auto_refresh_interval_seconds(self) -> int:
        # Keep the expensive market-wide scan separate from 10-second LTP polling.
        return max(1, int(self._settings.s9_top_refresh_interval_seconds))

    def _schedule_s9_top_opportunities_refresh_if_due(self, *, limit: int, reason: str) -> dict[str, Any]:
        now = datetime.now(self._market_tz)
        interval = timedelta(seconds=self._s9_top_auto_refresh_interval_seconds())
        with self._s9_top_refresh_lock:
            next_due_at = self._s9_top_next_auto_refresh_at
            if next_due_at is not None and next_due_at.tzinfo is None:
                next_due_at = next_due_at.replace(tzinfo=self._market_tz)
            if self._s9_top_refresh_in_progress:
                return {
                    "status": "skipped",
                    "reason": "already_running",
                    "next_refresh_at": self._iso_or_none(next_due_at),
                }
            if next_due_at is not None and now < next_due_at:
                return {
                    "status": "skipped",
                    "reason": "not_due",
                    "next_refresh_at": next_due_at.isoformat(),
                }
            self._s9_top_refresh_in_progress = True
            self._s9_top_next_auto_refresh_at = now + interval
            next_due_at = self._s9_top_next_auto_refresh_at

        self._start_s9_top_opportunities_refresh_worker(
            limit=limit,
            reason=reason,
            scheduled_at=now,
        )
        return {
            "status": "scheduled",
            "reason": reason,
            "next_refresh_at": next_due_at.isoformat(),
        }

    def _schedule_s9_top_opportunities_refresh(self, *, limit: int, reason: str) -> None:
        with self._s9_top_refresh_lock:
            if self._s9_top_refresh_in_progress:
                logger.info("S9_TOP_BACKGROUND_REFRESH already_running reason=%s", reason)
                return
            self._s9_top_refresh_in_progress = True

        self._start_s9_top_opportunities_refresh_worker(
            limit=limit,
            reason=reason,
            scheduled_at=datetime.now(self._market_tz),
        )

    def _start_s9_top_opportunities_refresh_worker(
        self,
        *,
        limit: int,
        reason: str,
        scheduled_at: datetime,
    ) -> None:
        def worker() -> None:
            next_due_at = scheduled_at + timedelta(seconds=self._s9_top_auto_refresh_interval_seconds())
            try:
                logger.info(
                    "S9_TOP_BACKGROUND_REFRESH started reason=%s scheduled_at=%s next_due_at=%s",
                    reason,
                    scheduled_at.isoformat(),
                    next_due_at.isoformat(),
                )
                self.refresh_s9_top_opportunities(limit=limit)
                with self._s9_top_refresh_lock:
                    self._s9_top_last_full_refresh_at = scheduled_at
                    self._s9_top_next_auto_refresh_at = next_due_at
                logger.info(
                    "S9_TOP_BACKGROUND_REFRESH completed reason=%s scheduled_at=%s next_due_at=%s",
                    reason,
                    scheduled_at.isoformat(),
                    next_due_at.isoformat(),
                )
            except Exception:  # noqa: BLE001
                with self._s9_top_refresh_lock:
                    self._s9_top_next_auto_refresh_at = datetime.now(self._market_tz)
                logger.exception("S9_TOP_BACKGROUND_REFRESH failed reason=%s", reason)
            finally:
                with self._s9_top_refresh_lock:
                    self._s9_top_refresh_in_progress = False

        Thread(target=worker, name="s9-top-opportunities-refresh", daemon=True).start()

    def get_swing_levels(
        self,
        *,
        symbol: str | None = None,
        timeframe: str = "15m",
        swing_lookback: int = 2,
        limit: int = 4,
    ) -> dict[str, Any]:
        selected_symbol = normalize_market_symbol(symbol or self._settings.underlying_symbol)
        if timeframe != "15m":
            raise ValueError("Swing levels are currently supported only for the 15m timeframe.")
        if swing_lookback < 1:
            raise ValueError("Swing lookback must be at least 1.")

        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(MarketCandle)
                    .where(MarketCandle.symbol == selected_symbol, MarketCandle.timeframe == timeframe)
                    .order_by(MarketCandle.candle_time.asc())
                )
            )

        if not rows:
            return {
                "symbol": selected_symbol,
                "timeframe": timeframe,
                "swing_lookback": int(swing_lookback),
                "count": 0,
                "current_price": None,
                "support": None,
                "resistance": None,
                "swing_highs": [],
                "swing_lows": [],
            }

        frame = pd.DataFrame(
            [
                {
                    "candle_time": row.candle_time,
                    "open": float(row.open_price),
                    "high": float(row.high_price),
                    "low": float(row.low_price),
                    "close": float(row.close_price),
                    "volume": float(row.volume),
                }
                for row in rows
            ]
        ).set_index("candle_time")

        highs = frame["high"].astype(float)
        lows = frame["low"].astype(float)
        current_price = float(frame["close"].iloc[-1])

        swing_highs: list[dict[str, Any]] = []
        swing_lows: list[dict[str, Any]] = []
        for idx in range(swing_lookback, len(frame) - swing_lookback):
            high = float(highs.iloc[idx])
            low = float(lows.iloc[idx])
            if high <= float(highs.iloc[idx - swing_lookback : idx].max()):
                continue
            if high <= float(highs.iloc[idx + 1 : idx + 1 + swing_lookback].max()):
                continue

            timestamp = frame.index[idx]
            candle_time = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else pd.Timestamp(timestamp).to_pydatetime()
            swing_highs.append({"time": candle_time.isoformat(), "price": round(high, 2)})

        for idx in range(swing_lookback, len(frame) - swing_lookback):
            low = float(lows.iloc[idx])
            if low >= float(lows.iloc[idx - swing_lookback : idx].min()):
                continue
            if low >= float(lows.iloc[idx + 1 : idx + 1 + swing_lookback].min()):
                continue

            timestamp = frame.index[idx]
            candle_time = timestamp.to_pydatetime() if isinstance(timestamp, pd.Timestamp) else pd.Timestamp(timestamp).to_pydatetime()
            swing_lows.append({"time": candle_time.isoformat(), "price": round(low, 2)})

        recent_highs = swing_highs[-max(0, int(limit)) :]
        recent_lows = swing_lows[-max(0, int(limit)) :]

        support_candidates = [item["price"] for item in swing_lows if item["price"] <= current_price]
        resistance_candidates = [item["price"] for item in swing_highs if item["price"] >= current_price]

        support = max(support_candidates) if support_candidates else (recent_lows[-1]["price"] if recent_lows else None)
        resistance = min(resistance_candidates) if resistance_candidates else (recent_highs[-1]["price"] if recent_highs else None)

        return {
            "symbol": selected_symbol,
            "timeframe": timeframe,
            "swing_lookback": int(swing_lookback),
            "count": len(recent_highs) + len(recent_lows),
            "current_price": round(current_price, 2),
            "support": support,
            "resistance": resistance,
            "swing_highs": recent_highs,
            "swing_lows": recent_lows,
        }

    def get_alerts(
        self,
        *,
        limit: int = 100,
        screeners: set[str] | None = None,
        actions: set[str] | None = None,
    ) -> dict[str, Any]:
        cache_key = "dashboard:alerts"
        cached = self._cache.get_json(cache_key)
        if cached is not None:
            payload = cached if isinstance(cached, dict) else {"run": None, "count": 0, "items": []}
            return self._filter_alert_payload(payload, limit=limit, screeners=screeners, actions=actions)

        with SessionLocal() as db:
            run = self._latest_completed_run(db)
            if run is None:
                payload = {
                    "run": None,
                    "count": 0,
                    "items": [],
                }
                self._cache.set_json(cache_key, payload)
                return self._filter_alert_payload(payload, limit=limit, screeners=screeners, actions=actions)

            rows = list(
                db.scalars(
                    select(AlertEvent)
                    .where(AlertEvent.run_id == run.id)
                    .order_by(AlertEvent.confidence.desc(), AlertEvent.id.asc())
                    .limit(300 if (screeners or actions) else limit)
                )
            )

            payload = {
                "run": self._serialize_run(run),
                "count": len(rows),
                "items": [self._serialize_alert_row(row) for row in rows],
            }
            self._cache.set_json(cache_key, payload)
            return self._filter_alert_payload(payload, limit=limit, screeners=screeners, actions=actions)

    def get_alerts_report(
        self,
        *,
        day: date | None = None,
        limit: int = 5000,
        screeners: set[str] | None = None,
        actions: set[str] | None = None,
    ) -> dict[str, Any]:
        day_market = day
        if day_market is None:
            day_market = datetime.now(self._market_tz).date()

        start_db = datetime.combine(day_market, self._market_open)
        end_db = datetime.combine(day_market, self._market_close)

        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(AlertEvent)
                    .where(AlertEvent.created_at >= start_db, AlertEvent.created_at <= end_db)
                    .order_by(AlertEvent.created_at.asc(), AlertEvent.id.asc())
                    .limit(max(0, int(limit)))
                )
            )

            payload = {
                "day": day_market.isoformat(),
                "window": {
                    "timezone": self._settings.market_timezone,
                    "market_open": self._settings.market_open_time,
                    "market_close": self._settings.market_close_time,
                    "start_ist": start_db.isoformat(),
                    "end_ist": end_db.isoformat(),
                },
                "count": len(rows),
                "items": [self._serialize_alert_row(row) for row in rows],
            }
            # Apply the same filtering semantics as /alerts (by action & screener(s)).
            return self._filter_alert_payload(payload, limit=limit, screeners=screeners, actions=actions)

    def get_latest_run(self) -> dict[str, Any] | None:
        with SessionLocal() as db:
            run = db.scalar(select(RefreshRun).order_by(RefreshRun.id.desc()).limit(1))
            if run is None:
                return None
            return self._serialize_run(run)

    def is_market_open(self, now_market: datetime | None = None) -> bool:
        now = now_market or datetime.now(self._market_tz)
        if now.weekday() >= 5:
            return False

        current = now.timetz().replace(tzinfo=None)
        return self._market_open <= current <= self._market_close

    @staticmethod
    def _update_s9_payload_ltp(
        payload: dict[str, Any],
        *,
        ltp_by_contract: dict[tuple[float, str], float],
        updated_at: str,
    ) -> int:
        updated = 0
        scanned = payload.get("scanned_strikes") if isinstance(payload.get("scanned_strikes"), list) else []
        for row in scanned:
            if not isinstance(row, dict):
                continue
            try:
                key = (float(row.get("strike")), str(row.get("option_type") or "").upper())
            except (TypeError, ValueError):
                continue
            ltp = ltp_by_contract.get(key)
            if ltp is None:
                continue
            row["ltp"] = ltp
            row["ltp_updated_at"] = updated_at
            updated += 1

        selected_strike = (
            payload.get("selected_strike")
            or payload.get("final_strike")
            or payload.get("strike")
            or payload.get("evaluated_strike")
        )
        selected_option_type = (
            payload.get("selected_option_type")
            or payload.get("final_option_type")
            or payload.get("option_type")
            or payload.get("evaluated_option_type")
        )
        try:
            selected_key = (float(selected_strike), str(selected_option_type or "").upper())
        except (TypeError, ValueError):
            selected_key = None
        if selected_key is not None:
            selected_ltp = ltp_by_contract.get(selected_key)
            if selected_ltp is not None:
                payload["ltp"] = selected_ltp
                payload["ltp_updated_at"] = updated_at
                updated += 1

        return updated

    def _refresh_cache(self, *, db_payload: dict[str, Any], run_id: int) -> None:
        # Cache TTL should exceed refresh interval so UI doesn't see gaps/stale fallbacks
        # between refresh cycles (especially when using in-memory cache instead of Redis).
        ttl_seconds = max(
            int(self._settings.redis_ttl_seconds),
            int(self._settings.refresh_interval_seconds) * 2,
        )
        self._cache.set_json("dashboard:last_refresh", db_payload, ttl_seconds=ttl_seconds)

        with SessionLocal() as db:
            run_obj = db.get(RefreshRun, run_id)
            for code in ("S1", "S9"):
                rows = list(
                    db.scalars(
                        select(ScreenerResult)
                        .where(ScreenerResult.run_id == run_id, ScreenerResult.screener_code == code)
                        .order_by(ScreenerResult.confidence.desc())
                    )
                )
                if code == "S1":
                    rows = [row for row in rows if self._is_selected_index_symbol(row.symbol)]
                payload = {
                    "screener": code,
                    "run": self._serialize_run(run_obj),
                    "count": len(rows),
                    "items": [self._serialize_screener_row(row) for row in rows],
                }
                self._cache.set_json(self._screener_cache_key(code), payload, ttl_seconds=ttl_seconds)

            alert_rows = list(
                db.scalars(
                    select(AlertEvent)
                    .where(AlertEvent.run_id == run_id)
                    .order_by(AlertEvent.confidence.desc(), AlertEvent.id.asc())
                )
            )
            alerts_payload = {
                "run": self._serialize_run(run_obj),
                "count": len(alert_rows),
                "items": [self._serialize_alert_row(row) for row in alert_rows],
            }
            self._cache.set_json("dashboard:alerts", alerts_payload, ttl_seconds=ttl_seconds)
            
    def _store_screener_rows(self, *, db, run_id: int, rows: dict[str, list[ScreenerSignal]]) -> None:
        for code, signals in rows.items():
            for row in signals:
                if code.upper() == "S1" and not self._is_selected_index_symbol(row.symbol):
                    logger.info("Skipping non-selected-index S1 row during storage: %s", row.symbol)
                    continue
                if code.upper() == "S9":
                    self._apply_s9_first_signal_gtp_snapshot(db=db, row=row)

                screener_result = ScreenerResult(
                    run_id=run_id,
                    screener_code=code,
                    symbol=row.symbol,
                    signal=row.signal,
                    confidence=row.confidence,
                    reason=row.reason,
                    payload=row.payload,
                )
                db.add(screener_result)
                if code.upper() == "S9":
                    self._store_s9_best_strike_row(
                        db=db,
                        run_id=run_id,
                        screener_result=screener_result,
                        row=row,
                    )
                    self._store_s9_filter_rows(
                        db=db,
                        run_id=run_id,
                        screener_result=screener_result,
                        row=row,
                    )

    def _store_s9_best_strike_row(self, *, db, run_id: int, screener_result: ScreenerResult, row: ScreenerSignal) -> None:
        payload = row.payload if isinstance(row.payload, dict) else {}
        best = payload.get("best_scanned_contract_evaluation")
        if not isinstance(best, dict):
            best = payload.get("selected_contract_evaluation")
        if not isinstance(best, dict):
            return

        db.flush()

        signal_time = self._parse_optional_datetime(payload.get("signal_time"))
        selected_strike = self._optional_float(
            payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike")
        )
        selected_option_type = str(
            payload.get("selected_option_type") or payload.get("final_option_type") or payload.get("option_type") or ""
        ).upper()
        best_strike = self._optional_float(best.get("strike"))
        best_option_type = str(best.get("option_type") or payload.get("option_type") or "").upper()

        db.add(
            S9BestStrikeResult(
                run_id=run_id,
                screener_result_id=screener_result.id,
                symbol=row.symbol,
                signal=row.signal,
                option_type=best_option_type or None,
                strike=best_strike,
                option_symbol=best.get("option_symbol"),
                score=self._optional_float(best.get("score")) or 0.0,
                max_score=self._optional_float(best.get("max_score")),
                raw_score=self._optional_float(best.get("raw_score")),
                raw_max_score=self._optional_float(best.get("raw_max_score")),
                passed_count=self._optional_int(best.get("passed_count")),
                total_filters=self._optional_int(best.get("total_filters")),
                confirmed=bool(best.get("confirmed")),
                is_selected=(
                    best_strike is not None
                    and selected_strike is not None
                    and best_option_type == selected_option_type
                    and float(best_strike) == float(selected_strike)
                ),
                selection_reason=payload.get("selection_reason"),
                strike_selection_mode=best.get("strike_selection_mode") or payload.get("strike_selection_mode"),
                distance_from_atm=self._optional_float(best.get("distance_from_atm")),
                signal_time=signal_time,
                data=best,
            )
        )

    def _store_s9_filter_rows(self, *, db, run_id: int, screener_result: ScreenerResult, row: ScreenerSignal) -> None:
        payload = row.payload if isinstance(row.payload, dict) else {}
        scanned_strikes = payload.get("scanned_strikes") if isinstance(payload.get("scanned_strikes"), list) else []
        if not scanned_strikes:
            selected = payload.get("selected_contract_evaluation")
            best = payload.get("best_scanned_contract_evaluation")
            scanned_strikes = [item for item in (selected, best) if isinstance(item, dict)]
        if not scanned_strikes:
            return

        db.flush()

        signal_time = self._parse_optional_datetime(payload.get("signal_time"))
        selected_strike = self._optional_float(
            payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike")
        )
        selected_option_type = str(
            payload.get("selected_option_type") or payload.get("final_option_type") or payload.get("option_type") or ""
        ).upper()
        seen: set[tuple[float | None, str]] = set()

        for strike_payload in scanned_strikes:
            if not isinstance(strike_payload, dict):
                continue
            strike = self._optional_float(strike_payload.get("strike"))
            option_type = str(strike_payload.get("option_type") or payload.get("option_type") or "").upper()
            if strike is None or option_type not in {"CE", "PE"}:
                logger.warning(
                    "Skipping invalid S9 consolidated row run=%s symbol=%s strike=%r option_type=%r",
                    run_id,
                    row.symbol,
                    strike,
                    option_type,
                )
                continue
            identity = (strike, option_type)
            if identity in seen:
                continue
            seen.add(identity)
            filters = self._s9_strike_filters(strike_payload)
            filter_data = deepcopy(strike_payload)
            filter_data.pop("ltp", None)
            sweep_pass = self._sweep_ema9_passed(filters)
            values = {
                "screener_result_id": screener_result.id,
                "signal": row.signal,
                "effective_direction": payload.get("effective_direction"),
                "effective_signal": payload.get("effective_signal"),
                "option_symbol": strike_payload.get("option_symbol") or payload.get("option_symbol") or payload.get("evaluated_option_symbol"),
                "passed": bool(strike_payload.get("all_filters_passed") or strike_payload.get("confirmed")),
                "reason": str(strike_payload.get("rejection_reason") or payload.get("rejection_reason") or ""),
                "data": filter_data,
                # Legacy boolean aliases remain populated for compatibility.
                "sweep": self._filter_passed(filters, "sweep"),
                "ema9": self._filter_passed(filters, "ema9"),
                "stoch_rsi": self._filter_passed(filters, "stoch_rsi"),
                "supertrend": self._filter_passed(filters, "supertrend"),
                "delta": self._filter_passed(filters, "delta"),
                "pcr": self._filter_passed(filters, "pcr"),
                "vwap": self._filter_passed(filters, "vwap"),
                "order_book": self._filter_passed(filters, "order_book"),
                "volume_breakout": self._filter_passed(filters, "volume_breakout"),
                "sweep_ema9_pass": sweep_pass,
                "sweep_ema9_value": self._sweep_ema9_value(filters),
                "stoch_rsi_pass": self._filter_passed(filters, "stoch_rsi"),
                "stoch_rsi_value": self._filter_value(filters, "stoch_rsi"),
                "supertrend_pass": self._filter_passed(filters, "supertrend"),
                "supertrend_value": self._filter_value(filters, "supertrend"),
                "delta_pass": self._filter_passed(filters, "delta"),
                "delta_value": self._filter_value(filters, "delta"),
                "pcr_pass": self._filter_passed(filters, "pcr"),
                "pcr_value": self._filter_value(filters, "pcr"),
                "vwap_pass": self._filter_passed(filters, "vwap"),
                "vwap_value": self._filter_value(filters, "vwap"),
                "order_book_pass": self._filter_passed(filters, "order_book"),
                "order_book_value": self._filter_value(filters, "order_book"),
                "volume_breakout_pass": self._filter_passed(filters, "volume_breakout"),
                "volume_breakout_value": self._filter_value(filters, "volume_breakout"),
                "score": self._optional_float(strike_payload.get("score")),
                "max_score": self._optional_float(strike_payload.get("max_score")),
                "passed_count": self._optional_int(
                    self._first_not_none(strike_payload.get("passed_filter_count"), strike_payload.get("passed_count"))
                ),
                "total_filters": self._optional_int(
                    self._first_not_none(strike_payload.get("required_filter_count"), strike_payload.get("total_filters"))
                ),
                "is_best": bool(strike_payload.get("is_best")),
                # Keep the scan-time option premium available while the
                # asynchronous LTP poll is running. The poll overwrites this
                # value as soon as it receives a newer option chain.
                "ltp": self._optional_float(strike_payload.get("ltp")),
                "is_selected": (
                    bool(strike_payload.get("is_selected"))
                    or (
                        selected_strike is not None
                        and option_type == selected_option_type
                        and float(strike) == float(selected_strike)
                    )
                ),
                "rejection_reason": payload.get("rejection_reason"),
                "signal_time": signal_time,
            }
            existing = db.scalar(
                select(S9FilterResult).where(
                    S9FilterResult.run_id == run_id,
                    S9FilterResult.symbol == row.symbol,
                    S9FilterResult.strike == strike,
                    S9FilterResult.option_type == option_type,
                    S9FilterResult.filter_name == "strike",
                )
            )
            if existing is None:
                db.add(
                    S9FilterResult(
                        run_id=run_id,
                        symbol=row.symbol,
                        strike=strike,
                        option_type=option_type,
                        filter_name="strike",
                        **values,
                    )
                )
            else:
                for field, value in values.items():
                    setattr(existing, field, value)

    @staticmethod
    def _s9_strike_filters(strike_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw_filters = strike_payload.get("filters")
        if isinstance(raw_filters, dict):
            return {str(key): value for key, value in raw_filters.items() if isinstance(value, dict)}
        if isinstance(raw_filters, list):
            return {
                str(item.get("key")): item
                for item in raw_filters
                if isinstance(item, dict) and item.get("key")
            }
        return {}

    @staticmethod
    def _filter_passed(filters: dict[str, dict[str, Any]], key: str) -> bool | None:
        item = filters.get(key)
        if item is None and key == "sweep":
            item = filters.get("ema9")
        data = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), dict) else {}
        if not isinstance(item, dict) or bool(data.get("ignored")):
            return None
        return bool(item.get("passed"))

    @classmethod
    def _sweep_ema9_passed(cls, filters: dict[str, dict[str, Any]]) -> bool | None:
        results = [cls._filter_passed(filters, key) for key in ("sweep", "ema9") if key in filters]
        active_results = [value for value in results if value is not None]
        return all(active_results) if active_results else None

    @classmethod
    def _sweep_ema9_value(cls, filters: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        values = {key: cls._filter_value(filters, key) for key in ("sweep", "ema9") if key in filters}
        return {key: value for key, value in values.items() if value is not None} or None

    @staticmethod
    def _filter_value(filters: dict[str, dict[str, Any]], key: str) -> Any:
        item = filters.get(key)
        if not isinstance(item, dict):
            return None
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if bool(data.get("ignored")):
            return None
        value = deepcopy(data)
        if item.get("actual_value") is not None:
            value["actual_value"] = item.get("actual_value")
        return value or item.get("actual_value")

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        return next((value for value in values if value is not None), None)

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _apply_s9_first_signal_gtp_snapshot(self, *, db, row: ScreenerSignal) -> None:
        payload = row.payload if isinstance(row.payload, dict) else None
        if payload is None or not self._is_s9_qualifying_signal(row):
            return
        item = {"symbol": row.symbol, "payload": payload}
        item_key = self._s9_gtp_identity(item)
        current_snapshot = self._s9_gtp_snapshot(item)
        if not item_key or not current_snapshot:
            return
        historical_snapshot = self._find_s9_first_signal_gtp_snapshot(
            db=db,
            symbol=row.symbol,
            identity=item_key,
        )
        snapshot = historical_snapshot or current_snapshot
        for snapshot_key in ("gtp", "gtp_time", "gtp_pass", "gtp_pass_count", "gtp_total_filters"):
            if snapshot_key in snapshot:
                payload[snapshot_key] = snapshot[snapshot_key]

    def _find_s9_first_signal_gtp_snapshot(self, *, db, symbol: str, identity: str) -> dict[str, Any] | None:
        today = datetime.now(self._market_tz).date()
        start = datetime.combine(today, time.min)
        end = start + timedelta(days=1)
        rows = db.scalars(
            select(ScreenerResult)
            .where(
                ScreenerResult.screener_code == "S9",
                ScreenerResult.symbol == symbol,
                ScreenerResult.created_at >= start,
                ScreenerResult.created_at < end,
            )
            .order_by(ScreenerResult.created_at.asc(), ScreenerResult.id.asc())
        )
        for existing in rows:
            existing_payload = existing.payload if isinstance(existing.payload, dict) else {}
            existing_signal = ScreenerSignal(
                screener="S9",
                symbol=existing.symbol,
                signal=existing.signal,
                confidence=existing.confidence,
                reason=existing.reason,
                payload=existing_payload,
            )
            if not self._is_s9_qualifying_signal(existing_signal):
                continue
            existing_item = {"symbol": existing.symbol, "payload": existing_payload}
            if self._s9_gtp_identity(existing_item) != identity:
                continue
            snapshot = self._s9_gtp_snapshot(existing_item)
            if snapshot:
                return snapshot
        return None

    @staticmethod
    def _is_s9_qualifying_signal(row: ScreenerSignal) -> bool:
        payload = row.payload if isinstance(row.payload, dict) else {}
        signal = str(row.signal or payload.get("signal") or "").upper()
        return bool(payload.get("confirmed")) and signal not in {"", "NONE", "NEUTRAL"}

    def _broadcast_s9_snapshot(self, *, run_id: int) -> None:
        if self._snapshot_broadcaster is None:
            return
        try:
            payload = self.get_screener_results(screener_code="S9", limit=5)
            self._snapshot_broadcaster.broadcast(
                {
                    "type": "s9_snapshot",
                    "version": int(run_id),
                    "as_of": datetime.utcnow().isoformat(),
                    "payload": payload,
                }
            )
        except Exception:  # noqa: BLE001
            logger.warning("S9 snapshot broadcast failed", exc_info=True)

    def _store_alert_rows(self, *, db, run_id: int, rows) -> None:
        for row in rows:
            db.add(
                AlertEvent(
                    run_id=run_id,
                    symbol=row.symbol,
                    alert_type=row.alert_type,
                    action=row.action,
                    confidence=row.confidence,
                    message=row.message,
                    payload=row.payload,
                    is_active=True,
                )
            )

    def _latest_completed_run(
        self,
        db,
        *,
        screener_code: str | None = None,
        symbol_scope: str | None = "selected",
    ) -> RefreshRun | None:
        stmt = select(RefreshRun).where(RefreshRun.status == "completed")
        if screener_code:
            code = screener_code.upper()
            stmt = (
                stmt.join(ScreenerResult, ScreenerResult.run_id == RefreshRun.id)
                .where(ScreenerResult.screener_code == code)
            )
            if symbol_scope:
                symbol = normalize_market_symbol(
                    symbol_scope
                    if symbol_scope != "selected"
                    else self._settings.underlying_symbol or self._settings.nifty_index_symbol
                )
                stmt = stmt.where(ScreenerResult.symbol == symbol)
        return db.scalar(stmt.order_by(RefreshRun.id.desc()).limit(1))

    @staticmethod
    def _serialize_run(run: RefreshRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        return {
            "id": run.id,
            "trigger": run.trigger,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "error": run.error_message,
        }

    @classmethod
    def _serialize_screener_row(cls, row: ScreenerResult) -> dict[str, Any]:
        payload = dict(row.payload or {})
        if row.screener_code.upper() == "S9":
            stored_filter_rows = [
                cls._serialize_s9_filter_result(filter_row)
                for filter_row in sorted(
                    (
                        filter_row
                        for filter_row in (row.s9_filter_results or [])
                        if filter_row.filter_name == "strike"
                    ),
                    key=lambda item: (
                        float("inf") if item.strike is None else float(item.strike),
                        str(item.option_type or ""),
                        item.id,
                    ),
                )
            ]
            if stored_filter_rows:
                payload["stored_filter_rows"] = stored_filter_rows
                payload["scanned_strikes"] = stored_filter_rows
                selected = next((item for item in stored_filter_rows if item.get("is_selected")), None)
                if selected is not None:
                    payload["stored_filter_row"] = selected
                    payload["filters"] = selected.get("filters") or payload.get("filters") or {}
        return {
            "id": row.id,
            "symbol": row.symbol,
            "signal": row.signal,
            "confidence": round(float(row.confidence), 4),
            "reason": row.reason,
            "payload": payload,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _serialize_s9_filter_result(row: S9FilterResult) -> dict[str, Any]:
        filters = {
            key: {"key": key, "passed": value}
            for key, value in {
                "sweep": row.sweep_ema9_pass if row.sweep_ema9_pass is not None else row.sweep,
                "ema9": row.ema9,
                "stoch_rsi": row.stoch_rsi_pass if row.stoch_rsi_pass is not None else row.stoch_rsi,
                "supertrend": row.supertrend_pass if row.supertrend_pass is not None else row.supertrend,
                "delta": row.delta_pass if row.delta_pass is not None else row.delta,
                "pcr": row.pcr_pass if row.pcr_pass is not None else row.pcr,
                "vwap": row.vwap_pass if row.vwap_pass is not None else row.vwap,
                "order_book": row.order_book_pass if row.order_book_pass is not None else row.order_book,
                "volume_breakout": row.volume_breakout_pass if row.volume_breakout_pass is not None else row.volume_breakout,
            }.items()
            if value is not None
        }
        data = deepcopy(row.data) if isinstance(row.data, dict) else {}
        data.pop("ltp", None)
        source_filters = data.get("filters") if isinstance(data.get("filters"), list) else []
        if source_filters:
            for item in source_filters:
                if not isinstance(item, dict) or not item.get("key"):
                    continue
                key = str(item["key"])
                filters[key] = {**item, "passed": filters.get(key, {}).get("passed", item.get("passed"))}
        return {
            **data,
            "id": row.id,
            "strike": row.strike,
            "option_symbol": row.option_symbol,
            "option_type": row.option_type,
            "is_best": bool(row.is_best),
            "is_selected": bool(row.is_selected),
            # This is the last known LTP from either the full scan or the
            # lightweight LTP refresh. It must survive result serialization
            # so the UI never clears the price to '-' between refreshes.
            "ltp": row.ltp,
            "all_filters_passed": bool(row.passed),
            "passed_filter_count": row.passed_count,
            "required_filter_count": row.total_filters,
            "score": row.score,
            "max_score": row.max_score,
            "filters": filters,
            "sweep_ema9_pass": row.sweep_ema9_pass if row.sweep_ema9_pass is not None else (row.sweep if row.sweep is not None else row.ema9),
            "sweep_ema9_value": row.sweep_ema9_value,
            "stoch_rsi_pass": row.stoch_rsi_pass if row.stoch_rsi_pass is not None else row.stoch_rsi,
            "stoch_rsi_value": row.stoch_rsi_value,
            "supertrend_pass": row.supertrend_pass if row.supertrend_pass is not None else row.supertrend,
            "supertrend_value": row.supertrend_value,
            "delta_pass": row.delta_pass if row.delta_pass is not None else row.delta,
            "delta_value": row.delta_value,
            "pcr_pass": row.pcr_pass if row.pcr_pass is not None else row.pcr,
            "pcr_value": row.pcr_value,
            "vwap_pass": row.vwap_pass if row.vwap_pass is not None else row.vwap,
            "vwap_value": row.vwap_value,
            "order_book_pass": row.order_book_pass if row.order_book_pass is not None else row.order_book,
            "order_book_value": row.order_book_value,
            "volume_breakout_pass": row.volume_breakout_pass if row.volume_breakout_pass is not None else row.volume_breakout,
            "volume_breakout_value": row.volume_breakout_value,
            "sweep": row.sweep if row.sweep is not None else row.ema9,
            "ema9": row.ema9,
            "stoch_rsi": row.stoch_rsi,
            "supertrend": row.supertrend,
            "delta": row.delta,
            "pcr": row.pcr,
            "vwap": row.vwap,
            "order_book": row.order_book,
            "volume_breakout": row.volume_breakout,
            "rejection_reason": row.rejection_reason or data.get("rejection_reason"),
            "signal_time": row.signal_time.isoformat() if row.signal_time else data.get("signal_time"),
        }

    def _screener_cache_key(self, code: str) -> str:
        symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        safe_symbol = symbol.replace(" ", "")
        return f"dashboard:screener:{code.lower()}:{safe_symbol}"

    @staticmethod
    def _s9_top_opportunities_cache_key() -> str:
        return "dashboard:screener:s9:top_opportunities"

    def _empty_s9_top_opportunities(self, *, limit: int) -> dict[str, Any]:
        mandatory = [
            self._s9_placeholder_item("NIFTY 50"),
            self._s9_placeholder_item("SENSEX"),
        ]
        return {
            "screener": "S9",
            "scope": "market_universe",
            "refresh_type": "none",
            "cache_version": None,
            "cache_run_id": None,
            "universe": self._s9_scan_universe(),
            "count": len(mandatory),
            "items": mandatory[: max(0, int(limit))],
            "runs": {},
            "errors": {},
            "diagnostics": {
                "candidate_count": 0,
                "unique_instrument_count": 0,
                "best_by_instrument": [],
                "selected": self._s9_item_summaries(mandatory),
                "previous_top": [],
                "new_top": self._s9_item_summaries(mandatory),
                "replacements": {"added": ["NIFTY 50", "SENSEX"], "removed": []},
            },
        }

    @staticmethod
    def _s9_placeholder_item(symbol: str) -> dict[str, Any]:
        symbol_key = normalize_market_symbol(symbol)
        return {
            "symbol": symbol_key,
            "signal": "neutral",
            "confidence": 0.0,
            "reason": "DATA_PENDING",
            "payload": {
                "underlying_symbol": symbol_key,
                "passed_count": 0,
                "total_filters": 0,
                "score": 0,
                "data_pending": True,
            },
        }

    def _s9_scan_universe(self, symbol: str | None = None) -> list[str]:
        if symbol:
            requested = normalize_market_symbol(symbol)
            return [requested] if requested in SUPPORTED_MARKET_SYMBOLS else []
        ordered: list[str] = []
        for market_symbol in (
            "NIFTY 50",
            "SENSEX",
            *tuple(self._settings.s9_scan_symbols or ()),
            *tuple(self._settings.nifty_symbols or SUPPORTED_MARKET_SYMBOLS),
        ):
            normalized = normalize_market_symbol(market_symbol)
            if normalized and normalized in SUPPORTED_MARKET_SYMBOLS and normalized not in ordered:
                ordered.append(normalized)
        return ordered

    def _s9_service_for_symbol(self, symbol: str) -> "RefreshService":
        normalized = normalize_market_symbol(symbol)
        security_id = self._settings.nifty50_security_map.get(normalized) or self._settings.dhan.nifty_security_id
        dhan = self._settings.dhan.model_copy(
            update={
                "nifty_security_id": security_id,
                "groww_trading_symbol": normalized,
                "groww_underlying_symbol": normalized,
            }
        )
        scoped_settings = self._settings.model_copy(
            deep=True,
            update={
                "underlying_symbol": normalized,
                "nifty_index_symbol": normalized,
                "dhan": dhan,
            },
        )
        return RefreshService(scoped_settings)

    @staticmethod
    def _s9_item_symbol(item: dict[str, Any]) -> str:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        return normalize_market_symbol(payload.get("underlying_symbol") or item.get("symbol") or "")

    @classmethod
    def _ensure_s9_mandatory_top_items(
        cls,
        items: list[Any],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        max_items = max(0, int(limit))
        valid_items = [item for item in items if isinstance(item, dict)]
        by_symbol = {cls._s9_item_symbol(item): item for item in valid_items}
        mandatory_symbols = ("NIFTY 50", "SENSEX")
        mandatory_items = [
            by_symbol.get(symbol) or cls._s9_placeholder_item(symbol)
            for symbol in mandatory_symbols
        ]
        mandatory_set = set(mandatory_symbols)
        remaining = [
            item
            for item in valid_items
            if cls._s9_item_symbol(item) not in mandatory_set
        ]
        dynamic_slots = max(0, max_items - len(mandatory_items))
        selected = [
            *mandatory_items,
            *sorted(remaining, key=cls._s9_score_value, reverse=True)[:dynamic_slots],
        ][:max_items]
        return sorted(selected, key=cls._s9_score_value, reverse=True)

    @classmethod
    def _ensure_s9_mandatory_items_preserve_order(
        cls,
        items: list[Any],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        max_items = max(0, int(limit))
        selected = [item for item in items if isinstance(item, dict)]
        present = {cls._s9_item_symbol(item) for item in selected}
        for symbol in ("NIFTY 50", "SENSEX"):
            if symbol not in present:
                selected.append(cls._s9_placeholder_item(symbol))
        return selected[:max_items]

    @staticmethod
    def _is_s9_dynamic_candidate_symbol(symbol: str) -> bool:
        normalized = normalize_market_symbol(symbol)
        if normalized in {"BANK NIFTY", "FINNIFTY"}:
            return True
        config = SYMBOL_CONFIG.get(normalized)
        return bool(config and config.get("instrument_type") == "stock")

    @staticmethod
    def _s9_score_value(item: dict[str, Any]) -> float:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        raw_score = payload.get("score")
        try:
            return float(raw_score) if raw_score is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _s9_score_key(cls, item: dict[str, Any]) -> float:
        return cls._s9_score_value(item)

    @classmethod
    def _rank_s9_top_opportunities(
        cls,
        items: list[dict[str, Any]],
        *,
        limit: int = 6,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ranked = sorted(
            (item for item in items if isinstance(item, dict)),
            key=cls._s9_score_value,
            reverse=True,
        )
        best_by_symbol: dict[str, dict[str, Any]] = {}
        for item in ranked:
            symbol = cls._s9_item_symbol(item)
            if symbol and symbol not in best_by_symbol:
                best_by_symbol[symbol] = item

        mandatory_symbols = ("NIFTY 50", "SENSEX")
        mandatory_items = [
            best_by_symbol.get(symbol) or cls._s9_placeholder_item(symbol)
            for symbol in mandatory_symbols
        ]
        remaining_items = [
            item
            for symbol, item in best_by_symbol.items()
            if symbol not in mandatory_symbols and cls._is_s9_dynamic_candidate_symbol(symbol)
        ]
        for item in [*mandatory_items, *remaining_items]:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            logger.info(
                "S9_TOP_UNIQUE_SCORE symbol=%s score=%s strike=%s option_type=%s",
                cls._s9_item_symbol(item),
                payload.get("score"),
                payload.get("evaluated_strike") or payload.get("selected_strike") or payload.get("strike"),
                payload.get("evaluated_option_type") or payload.get("selected_option_type") or payload.get("option_type"),
            )
        remaining_items.sort(key=cls._s9_score_value, reverse=True)
        dynamic_slots = max(0, int(limit) - len(mandatory_items))
        selected = (mandatory_items + remaining_items[:dynamic_slots])[: max(0, int(limit))]
        ordered = sorted(selected, key=cls._s9_score_value, reverse=True)
        best_summaries = cls._s9_item_summaries(best_by_symbol.values())
        selected_summaries = cls._s9_item_summaries(ordered)
        for item in ordered:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            logger.info(
                "S9_TOP_SELECTED symbol=%s strike=%s option_type=%s score=%s passed=%s/%s",
                cls._s9_item_symbol(item),
                payload.get("evaluated_strike") or payload.get("selected_strike") or payload.get("strike"),
                payload.get("evaluated_option_type") or payload.get("selected_option_type") or payload.get("option_type"),
                payload.get("score"),
                payload.get("passed_count"),
                payload.get("total_filters"),
            )
        return ordered, {
            "candidate_count": len(items),
            "unique_instrument_count": len(best_by_symbol),
            "best_by_instrument": best_summaries,
            "selected": selected_summaries,
        }

    @classmethod
    def _s9_item_summaries(cls, items: Any) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            summaries.append(
                {
                    "symbol": cls._s9_item_symbol(item),
                    "score": payload.get("score"),
                    "passed_count": payload.get("passed_count"),
                    "total_filters": payload.get("total_filters"),
                    "strike": payload.get("evaluated_strike") or payload.get("selected_strike") or payload.get("strike"),
                    "option_type": payload.get("evaluated_option_type") or payload.get("selected_option_type") or payload.get("option_type"),
                }
            )
        return summaries

    def _payload_matches_selected_index(self, items: list[Any]) -> bool:
        if not items:
            return True
        for item in items:
            if not isinstance(item, dict):
                return False
            if not self._is_selected_index_symbol(item.get("symbol")):
                return False
        return True

    def _is_selected_index_symbol(self, symbol: str | None) -> bool:
        if not symbol:
            return False
        expected = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        return normalize_market_symbol(symbol) == expected

    def _delete_stale_s1_rows(self, *, db) -> None:
        underlying_symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        db.execute(
            delete(ScreenerResult).where(
                ScreenerResult.screener_code == "S1",
                ScreenerResult.symbol != underlying_symbol,
            )
        )

    def _serialize_alert_row(self, row: AlertEvent) -> dict[str, Any]:
        company_name = None
        try:
            company_name = self._ingestion.resolve_company_name(symbol=row.symbol)
        except Exception:  # noqa: BLE001
            company_name = None

        return {
            "id": row.id,
            "symbol": row.symbol,
            "company_name": company_name,
            "alert_type": row.alert_type,
            "action": row.action,
            "confidence": round(float(row.confidence), 4),
            "message": row.message,
            "payload": row.payload or {},
            "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _parse_csv_set(value: str | None) -> set[str] | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        items = {part.strip().upper() for part in raw.split(",") if part.strip()}
        return items or None

    def _filter_alert_payload(
        self,
        payload: dict[str, Any],
        *,
        limit: int,
        screeners: set[str] | None,
        actions: set[str] | None,
    ) -> dict[str, Any]:
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        actions_lower = {a.strip().lower() for a in actions} if actions else None

        def matches_screener(item: dict[str, Any]) -> bool:
            if not screeners:
                return True
            pay = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            src = str(pay.get("source_screener") or "").strip().upper()
            if src and src in screeners:
                return True
            sources = pay.get("sources") if isinstance(pay.get("sources"), list) else []
            for s in sources:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("screener") or "").strip().upper()
                if code and code in screeners:
                    return True
            return False

        def matches_action(item: dict[str, Any]) -> bool:
            if not actions:
                return True
            act = str(item.get("action") or "").strip().lower()
            if actions_lower and act in actions_lower:
                return True
            normalized = act.upper() if act else ""
            return bool(normalized and actions and normalized in actions)

        filtered = [it for it in items if isinstance(it, dict) and matches_screener(it) and matches_action(it)]
        trimmed = filtered[: max(0, int(limit))]
        return {
            **payload,
            "items": trimmed,
            "count": len(filtered),
        }

    @staticmethod
    def _alerts_report_dir() -> str:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        return os.path.join(project_root, "logs")

    def _write_alerts_report_csv(self, payload: dict[str, Any], *, run_id: int) -> None:
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        if not items:
            return

        try:
            out_dir = self._alerts_report_dir()
            os.makedirs(out_dir, exist_ok=True)
            latest_path = os.path.join(out_dir, "alerts_report.csv")
            run_path = os.path.join(out_dir, f"alerts_report_run_{int(run_id)}.csv")
            day_market = datetime.now(self._market_tz).date()
            daily_path = os.path.join(out_dir, f"alerts_report_{day_market.isoformat()}.csv")

            def row_to_report(item: dict[str, Any]) -> dict[str, Any]:
                pay = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                sources = pay.get("sources") if isinstance(pay.get("sources"), list) else []
                screener_codes: list[str] = []
                for s in sources:
                    if not isinstance(s, dict):
                        continue
                    code = str(s.get("screener") or "").strip().upper()
                    if code and code not in screener_codes:
                        screener_codes.append(code)
                screener_codes.sort()

                return {
                    "id": item.get("id"),
                    "symbol": item.get("symbol"),
                    "company_name": item.get("company_name"),
                    "action": item.get("action"),
                    "alert_type": item.get("alert_type"),
                    "signal": pay.get("signal"),
                    "confidence": item.get("confidence"),
                    "message": item.get("message"),
                    "created_at": item.get("created_at"),
                    "source_screener": pay.get("source_screener"),
                    "screeners": ",".join(screener_codes),
                }

            rows = [row_to_report(it) for it in items if isinstance(it, dict)]
            if not rows:
                return

            fieldnames = list(rows[0].keys())

            def write_csv(path: str) -> None:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)

            write_csv(latest_path)
            write_csv(run_path)
            # Daily report overwrites with full-day (09:15–15:30) window.
            daily_payload = self.get_alerts_report(day=day_market, limit=10000)
            daily_items = daily_payload.get("items") if isinstance(daily_payload.get("items"), list) else []
            daily_rows = [row_to_report(it) for it in daily_items if isinstance(it, dict)]
            if daily_rows:
                daily_fieldnames = list(daily_rows[0].keys())
                with open(daily_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=daily_fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(daily_rows)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write alerts CSV report")

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hours, minutes = value.split(":")
        return time(hour=int(hours), minute=int(minutes))


