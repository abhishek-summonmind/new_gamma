from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import SYMBOL_CONFIG, Settings, get_settings, normalize_market_symbol
from ..models import MarketCandle, OptionOISnapshot
from .cache_service import CacheService
from .dhan_client import DhanClient
from .dhan_config_service import DhanConfigService
from .option_types import OptionChainSnapshot, OptionContract
from ..utils.time import to_ist_naive
import time as timer


logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    frames_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    option_chain: OptionChainSnapshot | None = None
    previous_option_map: dict[tuple[float, str], OptionOISnapshot] = field(default_factory=dict)
    fetched_symbols: list[str] = field(default_factory=list)
    failed_symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candle_writes: dict[str, int] = field(default_factory=lambda: {"inserted": 0, "updated": 0, "skipped": 0})


class DataIngestionService:
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._dhan = DhanClient(self._settings)
        self._scrip_master_df: pd.DataFrame | None = None
        self._symbol_security_map = None

    def _get_symbol_security_map(self):
        if self._symbol_security_map is None:
            self._symbol_security_map = self._resolve_symbol_security_map()

        return self._symbol_security_map

    def fetch_option_chain_snapshot(self) -> OptionChainSnapshot:
        runtime_settings = DhanConfigService(self._settings).apply_runtime_market_data_config(self._settings)
        if (
            runtime_settings.market_data_mode != self._settings.market_data_mode
            or runtime_settings.dhan.provider != self._settings.dhan.provider
        ):
            self._settings = runtime_settings
            self._dhan = DhanClient(runtime_settings)
        if self._settings.market_data_mode == "mock":
            return self._build_mock_option_chain(datetime.now(ZoneInfo(self._settings.market_timezone)))
        if not self._dhan.configured:
            raise RuntimeError("Market data credentials are missing.")
        return self._dhan.get_option_chain(
            self._settings.underlying_symbol or self._settings.nifty_index_symbol,
            depth=self._settings.dhan.option_chain_depth,
        )

    def _build_mock_option_chain(self, now_market: datetime) -> OptionChainSnapshot:
        selected_symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        mock_spot_by_symbol = {
            "NIFTY 50": 24500.0,
            "BANK NIFTY": 55500.0,
            "SENSEX": 80000.0,
            "FINNIFTY": 23500.0,
        }
        strike_step_by_symbol = {
            "NIFTY 50": 50,
            "BANK NIFTY": 100,
            "SENSEX": 100,
            "FINNIFTY": 50,
        }
        mock_spot = mock_spot_by_symbol.get(selected_symbol, 24500.0)
        strike_step = strike_step_by_symbol.get(selected_symbol, 50)
        configured_expiry = DhanConfigService(self._settings).get_effective_option_expiry()
        mock_expiry = configured_expiry or self._settings.dhan.option_expiry or now_market.date()
        atm_strike = round(mock_spot / strike_step) * strike_step
        mock_contracts: list[OptionContract] = []
        for offset in range(-8, 10):
            strike = atm_strike + (offset * strike_step)
            distance = abs(offset)
            for option_type in ("CE", "PE"):
                mock_contracts.append(
                    OptionContract(
                        security_id=f"MOCK-{selected_symbol.replace(' ', '')}-{strike}-{option_type}",
                        strike=float(strike),
                        option_type=option_type,
                        ltp=max(25.0, 220.0 - (distance * 12.0)),
                        oi=100000.0 + (distance * 2500.0),
                        oi_change=0.0,
                        volume=1000.0 + (distance * 100.0),
                        theta=-12.0,
                        bid_qty=700.0 if option_type == "CE" else 300.0,
                        ask_qty=300.0 if option_type == "CE" else 700.0,
                    )
                )
        return OptionChainSnapshot(
            underlying=selected_symbol,
            spot_price=mock_spot,
            atm_strike=atm_strike,
            expiry_date=mock_expiry,
            snapshot_time=now_market,
            contracts=tuple(mock_contracts),
        )

    def ingest(self, *, db: Session, run_id: int, now_market: datetime) -> IngestionResult:
        runtime_settings = DhanConfigService(self._settings).apply_runtime_market_data_config(self._settings)
        if (
            runtime_settings.market_data_mode != self._settings.market_data_mode
            or runtime_settings.dhan.provider != self._settings.dhan.provider
        ):
            self._settings = runtime_settings
            self._dhan = DhanClient(runtime_settings)
        if self._settings.market_data_mode == "mock":
            return self._ingest_mock(db=db, run_id=run_id, now_market=now_market)

        if not self._dhan.configured:
            if self._settings.dhan.provider == "groww":
                raise RuntimeError(
                    "Groww credentials are missing. Configure GROWW_ACCESS_TOKEN or "
                    "both GROWW_API_KEY and GROWW_API_SECRET."
                )
            raise RuntimeError("Dhan credentials are missing. Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")

        result = IngestionResult()
        fetch_from = now_market - timedelta(days=self._settings.intraday_fetch_days)
        auth_failed = False

        start = timer.perf_counter()
        symbol_map = self._get_symbol_security_map()

        equity_fetch_success = False

        for symbol, security_id in symbol_map.items():
            try:
                symbol_scan_start = timer.perf_counter()
                start = timer.perf_counter()
                frame = self._fetch_symbol_frame(
                    db=db,
                    symbol=symbol,
                    security_id=security_id,
                    fetch_from=fetch_from,
                    now_market=now_market,
                )
                print(
                    symbol,
                    "FETCH TOOK:",
                    round(timer.perf_counter() - start, 2)
                )

                if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                    result.failed_symbols.append(symbol)
                    logger.warning("%s candle fetch returned no valid data.", symbol)
                    continue

                print(
                    f"{symbol} FETCH SUCCESS | rows={len(frame)}"
                )
                latest = frame.iloc[-1]
                latest_ts = frame.index[-1] if len(frame.index) else None
                if latest_ts is not None:
                    market_tz = ZoneInfo(self._settings.market_timezone)
                    latest_ist = latest_ts.astimezone(market_tz)
                    print(
                        f"{symbol} LATEST CLOSED CANDLE RAW -> time_utc={latest_ts.isoformat()} time_ist={latest_ist.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                        f"open={float(latest['open']):.2f} high={float(latest['high']):.2f} "
                        f"low={float(latest['low']):.2f} close={float(latest['close']):.2f} volume={int(latest['volume'])}"
                    )

                start = timer.perf_counter()

                candle_write = self._replace_candles(
                    db=db,
                    run_id=run_id,
                    symbol=symbol,
                    security_id=security_id,
                    frame=frame,
                )
                for key, value in candle_write.items():
                    result.candle_writes[key] += value

                start = timer.perf_counter()
                frame = self._load_cached_symbol_frame(
                    db=db,
                    symbol=symbol,
                    max_rows=None,
                    since=fetch_from,
                )
                logger.info(
                    "CANDLE_DB_READ symbol=%s rows=%s duration_seconds=%.3f",
                    symbol,
                    len(frame),
                    timer.perf_counter() - start,
                )
                logger.info(
                    "TOTAL_SYMBOL_SCAN symbol=%s rows=%s duration_seconds=%.3f",
                    symbol,
                    len(frame),
                    timer.perf_counter() - symbol_scan_start,
                )

                print(
                    f"{symbol} INSERT SUCCESS | rows={len(frame)}"
                )

                result.frames_by_symbol[symbol] = frame
                result.fetched_symbols.append(symbol)
                equity_fetch_success = True
            except SQLAlchemyError:
                db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                cached_frame = self._fallback_cached_symbol_frame(
                    db=db,
                    symbol=symbol,
                    reason=message,
                )
                if cached_frame is not None:
                    result.frames_by_symbol[symbol] = cached_frame
                    result.fetched_symbols.append(symbol)
                    result.warnings.append(f"{symbol}: live candles unavailable; using cached DB candles.")
                    equity_fetch_success = True
                    continue
                is_auth_failure = "Dhan authentication failed" in message
                if is_auth_failure:
                    auth_failed = True
                    logger.warning("%s OHLCV ingestion skipped: %s", symbol, message)
                else:
                    logger.warning("OHLCV ingestion failed for %s", symbol, exc_info=True)
                result.failed_symbols.append(symbol)
                result.warnings.append(f"{symbol}: {message}")
                # Persist last failure reason for the UI/debug layers.
                try: 
                    self._settings  # keep type-checkers happy
                    CacheService(self._settings).set_json(
                        f"ingestion:last_error:{symbol.upper()}",
                        {"symbol": symbol.upper(), "error": str(exc), "as_of": now_market.isoformat()},
                    )
                except Exception:  # noqa: BLE001
                    pass

        try:
            if auth_failed:
                raise RuntimeError("Dhan authentication failed. Update DHAN_ACCESS_TOKEN with a fresh token.")
            start = timer.perf_counter()
            option_chain = self._dhan.get_option_chain(
                self._settings.underlying_symbol or self._settings.nifty_index_symbol,
                depth=self._settings.dhan.option_chain_depth,
            )
            print(
                f"{option_chain.underlying} OPTION CHAIN TOOK:",
                round(timer.perf_counter() - start, 2)
            )
            result.previous_option_map = self._load_previous_option_map(db=db, snapshot=option_chain)
            option_chain = self._with_oi_change_percent(option_chain, result.previous_option_map)
            self._store_option_chain(db=db, run_id=run_id, snapshot=option_chain)
            result.option_chain = option_chain
        except SQLAlchemyError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "Dhan authentication failed" in message:
                logger.warning("Index option chain skipped: %s", message)
            else:
                logger.warning("Index option chain fetch failed", exc_info=True)
            result.warnings.append(f"options: {message}")
            cached_option_chain = self._fallback_cached_option_chain(
                db=db,
                now_market=now_market,
                error=message,
                frames_by_symbol=result.frames_by_symbol,
                expected_identity=getattr(self._dhan, "last_groww_option_chain_identity", None),
            )
            if cached_option_chain is not None:
                result.option_chain = cached_option_chain
                result.previous_option_map = self._load_previous_option_map(
                    db=db,
                    snapshot=cached_option_chain,
                )
                result.warnings.append(
                    f"options: live chain unavailable; using recent cached snapshot from "
                    f"{cached_option_chain.snapshot_time.isoformat()}."
                )
                logger.warning(
                    "Using recent cached option chain underlying=%s expiry=%s snapshot_time=%s contracts=%s",
                    cached_option_chain.underlying,
                    cached_option_chain.expiry_date.isoformat(),
                    cached_option_chain.snapshot_time.isoformat(),
                    len(cached_option_chain.contracts),
                )
            else:
                raise RuntimeError(
                    f"Live option-chain fetch failed for {normalize_market_symbol(self._settings.underlying_symbol)}: {message}"
                ) from exc

        if not equity_fetch_success:
            index_symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
            if index_symbol not in result.frames_by_symbol:
                logger.warning("%s index candle ingestion failed; no candle frame available.", index_symbol)
                result.warnings.append(f"{index_symbol} candles unavailable.")

        return result

    def _ingest_mock(self, *, db: Session, run_id: int, now_market: datetime) -> IngestionResult:
        result = IngestionResult()
        symbols = self._settings.market_symbols
        selected_symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        mock_spot_by_symbol = {
            "NIFTY 50": 24500.0,
            "BANK NIFTY": 55500.0,
            "SENSEX": 80000.0,
            "FINNIFTY": 23500.0,
        }
        strike_step_by_symbol = {
            "NIFTY 50": 50,
            "BANK NIFTY": 100,
            "SENSEX": 100,
            "FINNIFTY": 50,
        }
        mock_spot = mock_spot_by_symbol.get(selected_symbol, 24500.0)
        strike_step = strike_step_by_symbol.get(selected_symbol, 50)
        configured_expiry = DhanConfigService(self._settings).get_effective_option_expiry()
        mock_expiry = configured_expiry or self._settings.dhan.option_expiry or now_market.date()

        for symbol in symbols:
            frame = self._build_mock_intraday_frame(
                symbol=symbol,
                now_market=now_market,
                hint_price=mock_spot,
            )
            candle_write = self._replace_candles(
                db=db,
                run_id=run_id,
                symbol=symbol,
                security_id=f"MOCK-{symbol}",
                frame=frame,
            )
            for key, value in candle_write.items():
                result.candle_writes[key] += value
            result.frames_by_symbol[symbol] = frame
            result.fetched_symbols.append(symbol)

        atm_strike = round(mock_spot / strike_step) * strike_step
        mock_contracts: list[OptionContract] = []
        # Supply 18 strike levels for each direction so the S9 scanner can
        # exercise its complete index contract-selection pipeline in mock mode.
        for offset in range(-8, 10):
            strike = atm_strike + (offset * strike_step)
            distance = abs(offset)
            for option_type in ("CE", "PE"):
                mock_contracts.append(
                    OptionContract(
                        security_id=f"MOCK-{selected_symbol.replace(' ', '')}-{strike}-{option_type}",
                        strike=float(strike),
                        option_type=option_type,
                        ltp=max(25.0, 220.0 - (distance * 12.0)),
                        oi=100000.0 + (distance * 2500.0),
                        oi_change=0.0,
                        volume=1000.0 + (distance * 100.0),
                        theta=-12.0,
                        bid_qty=700.0 if option_type == "CE" else 300.0,
                        ask_qty=300.0 if option_type == "CE" else 700.0,
                    )
                )
        result.option_chain = OptionChainSnapshot(
            underlying=selected_symbol,
            spot_price=mock_spot,
            atm_strike=atm_strike,
            expiry_date=mock_expiry,
            snapshot_time=now_market,
            contracts=tuple(mock_contracts),
        )

        result.warnings.append(
            f"USE_MOCK_DATA=true: using synthetic {selected_symbol} index OHLC frames "
            f"with configured expiry {mock_expiry.isoformat()}; live broker fetch disabled."
        )
        return result

    def _resolve_symbol_security_map(self) -> dict[str, str]:
        index_symbol = normalize_market_symbol(self._settings.underlying_symbol or self._settings.nifty_index_symbol)
        security_id = str(
            self._settings.nifty50_security_map.get(index_symbol)
            or self._settings.dhan.nifty_security_id
        ).strip()

        logger.info("Selected index symbol security map: %s -> %s", index_symbol, security_id)

        return {
            index_symbol: security_id,
        }

    def _auto_resolve_security_id(self, *, symbol: str) -> str | None:
        """
        Best-effort resolver for equity security IDs.

        Uses Dhan's public scrip master CSV (cached on disk) to map trading symbols
        to Security IDs when NIFTY50_SECURITY_MAP is not provided.
        """
        symbol_key = str(symbol).strip().upper()
        if not symbol_key:
            return None

        # Prefer explicit env mapping when present.
        existing = self._settings.nifty50_security_map.get(symbol_key)
        if existing:
            return str(existing).strip()

        try:
            df = self._load_scrip_master()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load Dhan scrip master for auto security-id mapping: %s", exc)
            return None

        if df is None or df.empty:
            return None

        # Candidate columns seen in Dhan compact/detailed masters.
        col_symbol = self._pick_column(df, candidates=("SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "SYMBOL", "SM_SYMBOL_NAME"))
        col_exch = self._pick_column(df, candidates=("EXCH_ID", "SEM_EXM_EXCH_ID", "EXCHANGE"))
        col_segment = self._pick_column(df, candidates=("SEGMENT", "SEM_SEGMENT", "EXCHANGE_SEGMENT"))
        col_instr_name = self._pick_column(df, candidates=("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "INSTRUMENT"))
        col_series = self._pick_column(df, candidates=("SEM_SERIES", "SERIES"))
        col_sec = self._pick_column(
            df,
            candidates=("SECURITY_ID", "SEM_SMST_SECURITY_ID", "SECURITYID", "SECURITYID "),
        )

        if not col_symbol or not col_sec:
            return None

        work = df
        if col_exch:
            work = work[work[col_exch].astype(str).str.upper().eq("NSE")]

        # Dhan compact scrip master uses coded segments:
        # - Equities are typically "E" (not "NSE_EQ")
        # - Derivatives are "D"
        # To avoid false matches (options/futures/bonds), prefer equity instrument + EQ series.
        if col_instr_name:
            work = work[work[col_instr_name].astype(str).str.upper().eq("EQUITY")]

        if col_segment:
            seg = work[col_segment].astype(str).str.upper()
            work = work[seg.isin({"E", "NSE_EQ"})]

        if col_series:
            series = work[col_series].astype(str).str.upper()
            work = work[series.eq("EQ")]

        if work.empty:
            return None

        # Normalization: Dhan master sometimes uses suffix "-EQ" or different punctuation.
        # We'll try a few variants.
        variants = [
            symbol_key,
            f"{symbol_key}-EQ",
            symbol_key.replace("&", "AND"),
        ]
        # Also support common NSE symbols where '-' is omitted.
        variants.append(symbol_key.replace("-", ""))

        sym_series = work[col_symbol].astype(str).str.upper().str.strip()
        for v in variants:
            hit = work[sym_series.eq(v)]
            if not hit.empty:
                val = str(hit.iloc[0][col_sec]).strip()
                return val or None

        # Fallback: contains match (avoid broad matches by requiring exact token boundary).
        try:
            hit = work[sym_series.str.fullmatch(symbol_key.replace("-", r"\-") + r"(-EQ)?", na=False)]
        except Exception:  # noqa: BLE001
            hit = pd.DataFrame()
        if not hit.empty:
            val = str(hit.iloc[0][col_sec]).strip()
            return val or None

        return None

    def _load_scrip_master(self) -> pd.DataFrame:
        if self._scrip_master_df is not None:
            return self._scrip_master_df

        cache_dir = os.path.join(os.path.dirname(__file__), "..", ".cache")
        cache_dir = os.path.abspath(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "dhan_api_scrip_master.csv")

        use_cache = False
        if os.path.exists(cache_path):
            age_seconds = (datetime.utcnow().timestamp() - os.path.getmtime(cache_path))
            # refresh daily
            use_cache = age_seconds < 24 * 3600

        if use_cache:
            df = pd.read_csv(cache_path, dtype=str)
            self._scrip_master_df = df
            return df

        url = "https://images.dhan.co/api-data/api-scrip-master.csv"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.content

        with open(cache_path, "wb") as f:
            f.write(content)

        df = pd.read_csv(cache_path, dtype=str)
        self._scrip_master_df = df
        return df

    def resolve_company_name(self, *, symbol: str) -> str | None:
        symbol_key = str(symbol or "").strip().upper()
        if not symbol_key:
            return None
        if normalize_market_symbol(symbol_key) == normalize_market_symbol(self._settings.nifty_index_symbol):
            return normalize_market_symbol(self._settings.nifty_index_symbol)

        try:
            df = self._load_scrip_master()
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None

        col_symbol = self._pick_column(df, candidates=("SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "SYMBOL", "SM_SYMBOL_NAME"))
        col_exch = self._pick_column(df, candidates=("EXCH_ID", "SEM_EXM_EXCH_ID", "EXCHANGE"))
        col_segment = self._pick_column(df, candidates=("SEGMENT", "SEM_SEGMENT", "EXCHANGE_SEGMENT"))
        col_instr_name = self._pick_column(df, candidates=("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "INSTRUMENT"))
        col_series = self._pick_column(df, candidates=("SEM_SERIES", "SERIES"))
        col_custom = self._pick_column(df, candidates=("SEM_CUSTOM_SYMBOL", "CUSTOM_SYMBOL", "SYMBOL_NAME"))
        col_long = self._pick_column(df, candidates=("SM_SYMBOL_NAME", "SEM_SYMBOL_NAME", "SYMBOL_LONG_NAME", "LONG_NAME", "NAME"))

        if not col_symbol:
            return None

        work = df
        if col_exch:
            # Prefer NSE for NIFTY50 equities.
            work = work[work[col_exch].astype(str).str.upper().eq("NSE")]

        if col_instr_name:
            work = work[work[col_instr_name].astype(str).str.upper().eq("EQUITY")]

        if col_segment:
            seg = work[col_segment].astype(str).str.upper()
            work = work[seg.isin({"E", "NSE_EQ"})]

        if col_series:
            series = work[col_series].astype(str).str.upper()
            work = work[series.eq("EQ")]

        if work.empty:
            return None

        variants = [
            symbol_key,
            f"{symbol_key}-EQ",
            symbol_key.replace("&", "AND"),
            symbol_key.replace("-", ""),
        ]

        sym_series = work[col_symbol].astype(str).str.upper().str.strip()
        for v in variants:
            hit = work[sym_series.eq(v)]
            if hit.empty:
                continue

            row = hit.iloc[0]
            for col in (col_long, col_custom):
                if not col:
                    continue
                value = str(row.get(col, "")).strip()
                if value:
                    return value
            return None

        return None

    @staticmethod
    def _pick_column(df: pd.DataFrame, *, candidates: tuple[str, ...]) -> str | None:
        cols = {str(c).strip().upper(): c for c in df.columns}
        for cand in candidates:
            key = str(cand).strip().upper()
            if key in cols:
                return str(cols[key])
        return None

    def _fetch_symbol_frame(
        self,
        *,
        db: Session,
        symbol: str,
        security_id: str,
        fetch_from: datetime,
        now_market: datetime,
    ) -> pd.DataFrame:
        exchange_segment = self._settings.dhan.equity_exchange_segment
        instrument = self._settings.dhan.equity_instrument

        required_rows = max(self._settings.lookback_candles * 6, 420)
        latest_candle = db.scalar(
            select(MarketCandle.candle_time)
            .where(MarketCandle.symbol == symbol, MarketCandle.timeframe == "1m")
            .order_by(MarketCandle.candle_time.desc())
            .limit(1)
        )
        stored_rows = db.scalar(
            select(func.count())
            .select_from(MarketCandle)
            .where(
                MarketCandle.symbol == symbol,
                MarketCandle.timeframe == "1m",
                MarketCandle.candle_time >= self._to_db_time(fetch_from),
            )
        ) or 0

        bootstrap = latest_candle is None or int(stored_rows) < required_rows
        request_from = fetch_from
        if bootstrap:
            timing_label = "HISTORICAL_BOOTSTRAP"
        else:
            latest_aware = latest_candle.replace(tzinfo=ZoneInfo(self._settings.market_timezone))
            request_from = latest_aware - timedelta(minutes=1)
            timing_label = "INCREMENTAL_CANDLE_FETCH"

        provider = str(getattr(self._settings.dhan, "provider", "dhan") or "dhan").strip().upper()
        print(
            f"{symbol} {provider} OHLC REQUEST ->",
            f"symbol={symbol}",
            f"security_id={security_id}",
            f"exchange_segment={exchange_segment}",
            f"instrument={instrument}",
            "interval=1",
            f"from_date={request_from.isoformat()}",
            f"to_date={now_market.isoformat()}",
        )

        start = timer.perf_counter()
        frame = self._dhan.get_intraday_ohlc(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument=instrument,
            from_date=request_from,
            to_date=now_market,
            interval_minutes=1,
            include_oi=False,
            trading_symbol=symbol,
        )

        logger.info(
            "%s symbol=%s rows=%s from=%s to=%s duration_seconds=%.3f",
            timing_label,
            symbol,
            len(frame) if isinstance(frame, pd.DataFrame) else 0,
            request_from.isoformat(),
            now_market.isoformat(),
            timer.perf_counter() - start,
        )

        return frame

    def _build_mock_intraday_frame(
        self,
        *,
        symbol: str,
        now_market: datetime,
        hint_price: float | None = None,
    ) -> pd.DataFrame:
        periods = max(self._settings.lookback_candles * 6, 420)
        base_price = float(hint_price or (120.0 + (sum(ord(ch) for ch in symbol) % 1800)))

        seed = abs(hash(symbol)) % (2**31 - 1)
        rng = Random(seed)

        market_now_utc = now_market.astimezone(ZoneInfo("UTC"))
        times = pd.date_range(end=market_now_utc, periods=periods, freq="1min", tz="UTC")

        closes: list[float] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        volumes: list[float] = []

        drift = ((seed % 9) - 4) * 0.00011
        phase = (seed % 29) / 5.0
        prev_close = base_price

        for i in range(periods):
            wave = math.sin((i / 11.0) + phase) * 0.0065
            micro = math.cos((i / 5.0) + (phase / 2.0)) * 0.0012
            trend = (i - (periods / 2.0)) * drift

            close_price = max(1.0, base_price * (1.0 + trend + wave + micro))
            open_price = prev_close
            high_price = max(open_price, close_price) * (1.0 + rng.uniform(0.0004, 0.0036))
            low_price = min(open_price, close_price) * (1.0 - rng.uniform(0.0004, 0.0032))
            volume = float(80_000 + (i % 75) * 1_700 + rng.randint(0, 22_000))

            opens.append(open_price)
            highs.append(high_price)
            lows.append(low_price)
            closes.append(close_price)
            volumes.append(max(1000.0, volume))
            prev_close = close_price

        frame = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=times,
        )
        return frame

    def _replace_candles(
        self,
        *,
        db: Session,
        run_id: int,
        symbol: str,
        security_id: str,
        frame: pd.DataFrame,
    ) -> dict[str, int]:
        summary = {"inserted": 0, "updated": 0, "skipped": 0}
        if frame.empty:
            return summary

        start = timer.perf_counter()
        rows_by_key: dict[tuple[str, str, datetime], dict[str, object]] = {}
        for timestamp, row in frame.iterrows():
            candle_time = self._to_db_time(timestamp)
            key = (symbol, "1m", candle_time)
            if key in rows_by_key:
                summary["skipped"] += 1
            rows_by_key[key] = {
                "run_id": run_id,
                "symbol": symbol,
                "security_id": security_id,
                "timeframe": "1m",
                "candle_time": candle_time,
                "open_price": float(row["open"]),
                "high_price": float(row["high"]),
                "low_price": float(row["low"]),
                "close_price": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
        rows = list(rows_by_key.values())
        if not rows:
            return summary

        statement = postgresql_insert(MarketCandle.__table__).values(rows)
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            index_elements=[
                MarketCandle.__table__.c.symbol,
                MarketCandle.__table__.c.timeframe,
                MarketCandle.__table__.c.candle_time,
            ],
            set_={
                "open_price": excluded.open_price,
                "high_price": excluded.high_price,
                "low_price": excluded.low_price,
                "close_price": excluded.close_price,
                "volume": excluded.volume,
                "run_id": excluded.run_id,
                "security_id": excluded.security_id,
            },
        ).returning(literal_column("(xmax = 0)").label("was_inserted"))

        try:
            flags = list(db.execute(statement).scalars())
        except Exception:
            # A failed SQL statement poisons the PostgreSQL transaction. Recover
            # immediately so callers never inherit a PendingRollback session.
            db.rollback()
            logger.exception(
                "market_candle_upsert_failed symbol=%s rows=%s rollback=true",
                symbol, len(rows),
            )
            raise

        summary["inserted"] = sum(1 for flag in flags if bool(flag))
        summary["updated"] = len(flags) - summary["inserted"]
        logger.info(
            "market_candle_upsert symbol=%s timeframe=1m rows=%s inserted=%s updated=%s skipped=%s duration_seconds=%.3f",
            symbol, len(rows), summary["inserted"], summary["updated"], summary["skipped"],
            timer.perf_counter() - start,
        )
        return summary

    def _fallback_cached_symbol_frame(self, *, db: Session, symbol: str, reason: str) -> pd.DataFrame | None:
        transient_markers = (
            "Groww request failed for /historical/candles",
            "Groww historical candles unavailable",
            "HTTP 500",
            "GA003",
            "Unable to serve request currently",
        )
        if not any(marker in reason for marker in transient_markers):
            return None

        frame = self._load_cached_symbol_frame(
            db=db,
            symbol=symbol,
            max_rows=max(self._settings.lookback_candles * 6, 420),
        )
        if frame.empty:
            return None
        logger.warning(
            "%s live OHLCV unavailable; using cached DB candles rows=%s reason=%s",
            symbol,
            len(frame),
            reason,
        )
        return frame

    def _load_cached_symbol_frame(
        self,
        *,
        db: Session,
        symbol: str,
        max_rows: int | None,
        since: datetime | None = None,
    ) -> pd.DataFrame:
        query = (
            select(MarketCandle)
            .where(MarketCandle.symbol == symbol, MarketCandle.timeframe == "1m")
            .order_by(MarketCandle.candle_time.desc())
        )
        if since is not None:
            query = query.where(MarketCandle.candle_time >= self._to_db_time(since))
        if max_rows is not None:
            query = query.limit(max_rows)
        rows = list(db.scalars(query))

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows.reverse()
        timestamps = pd.to_datetime([row.candle_time for row in rows], errors="coerce")
        if getattr(timestamps, "tz", None) is None:
            timestamps = timestamps.tz_localize(ZoneInfo(self._settings.market_timezone))
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [float(row.open_price) for row in rows],
                "high": [float(row.high_price) for row in rows],
                "low": [float(row.low_price) for row in rows],
                "close": [float(row.close_price) for row in rows],
                "volume": [float(row.volume) for row in rows],
            }
        )
        frame = frame.dropna(subset=["timestamp"])
        if frame.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        frame = frame.set_index("timestamp")
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame

    def _load_previous_option_map(
        self,
        *,
        db: Session,
        snapshot: OptionChainSnapshot,
    ) -> dict[tuple[float, str], OptionOISnapshot]:
        current_time = self._to_db_time(snapshot.snapshot_time)
        current_contract_oi = {(float(row.strike), row.option_type): float(row.oi) for row in snapshot.contracts}

        candidate_times = list(
            db.scalars(
                select(OptionOISnapshot.snapshot_time)
                .where(
                    OptionOISnapshot.underlying == snapshot.underlying,
                    OptionOISnapshot.expiry_date == snapshot.expiry_date,
                    OptionOISnapshot.snapshot_time < current_time,
                )
                .order_by(OptionOISnapshot.snapshot_time.desc())
                .distinct()
                .limit(8)
            )
        )

        fallback_map: dict[tuple[float, str], OptionOISnapshot] = {}

        for candidate_time in candidate_times:
            rows = list(
                db.scalars(
                    select(OptionOISnapshot).where(
                        OptionOISnapshot.underlying == snapshot.underlying,
                        OptionOISnapshot.expiry_date == snapshot.expiry_date,
                        OptionOISnapshot.snapshot_time == candidate_time,
                    )
                )
            )
            candidate_map = {(float(row.strike_price), row.option_type): row for row in rows}
            if not candidate_map:
                continue

            if not fallback_map:
                fallback_map = candidate_map

            has_difference = any(
                key in current_contract_oi and abs(current_contract_oi[key] - float(prev_row.oi)) > 0.5
                for key, prev_row in candidate_map.items()
            )

            if has_difference:
                return candidate_map

        return fallback_map

    def _fallback_cached_option_chain(
        self,
        *,
        db: Session,
        now_market: datetime,
        error: str,
        frames_by_symbol: dict[str, pd.DataFrame],
        expected_identity: tuple[str, str, date] | None = None,
    ) -> OptionChainSnapshot | None:
        """Return a recent DB snapshot only for transient Groww chain failures."""
        normalized_error = str(error or "").lower()
        transient_markers = (
            "underlying not found",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "ga000",
            "ga003",
            "unable to serve request currently",
        )
        if not any(marker in normalized_error for marker in transient_markers):
            return None

        underlying = normalize_market_symbol(
            self._settings.underlying_symbol or self._settings.nifty_index_symbol
        )
        if expected_identity is None:
            return None
        expected_exchange, expected_underlying, expected_expiry = expected_identity
        symbol_config = SYMBOL_CONFIG.get(underlying, {})
        configured_exchange = str(symbol_config.get("option_exchange") or "").upper()
        configured_underlying = str(symbol_config.get("option_underlying") or underlying).upper()
        if (
            str(expected_exchange).upper() != configured_exchange
            or str(expected_underlying).upper() != configured_underlying
        ):
            logger.error(
                "GROWW_INSTRUMENT_INVALID cache_lookup=true exchange=%s underlying=%s expiry=%s",
                expected_exchange,
                expected_underlying,
                expected_expiry.isoformat(),
            )
            return None
        latest = db.scalar(
            select(OptionOISnapshot)
            .where(
                OptionOISnapshot.underlying == underlying,
                OptionOISnapshot.expiry_date == expected_expiry,
            )
            .order_by(OptionOISnapshot.snapshot_time.desc())
            .limit(1)
        )
        if latest is None:
            return None

        latest_time = latest.snapshot_time
        market_now_naive = to_ist_naive(now_market)
        max_age_seconds = max(
            900,
            int(getattr(self._settings, "s9_top_refresh_interval_seconds", 180)) * 3,
        )
        if (market_now_naive - latest_time).total_seconds() > max_age_seconds:
            logger.warning(
                "GROWW_CACHE_STALE kind=option-chain exchange=%s underlying=%s expiry=%s "
                "cache_age_seconds=%.1f stale_after_seconds=%s",
                expected_exchange,
                expected_underlying,
                expected_expiry.isoformat(),
                (market_now_naive - latest_time).total_seconds(),
                max_age_seconds,
            )

        rows = list(
            db.scalars(
                select(OptionOISnapshot).where(
                    OptionOISnapshot.underlying == underlying,
                    OptionOISnapshot.expiry_date == latest.expiry_date,
                    OptionOISnapshot.snapshot_time == latest_time,
                )
            )
        )
        contracts = tuple(
            OptionContract(
                security_id=str(row.security_id),
                strike=float(row.strike_price),
                option_type=str(row.option_type).strip().upper(),
                ltp=float(row.ltp or 0.0),
                oi=float(row.oi or 0.0),
                oi_change=float(row.oi_change or 0.0),
                volume=float(row.traded_volume or 0.0),
            )
            for row in rows
            if float(row.ltp or 0.0) > 0
        )
        if not contracts:
            return None

        logger.warning(
            "GROWW_CACHE_FALLBACK kind=option-chain source=database exchange=%s underlying=%s "
            "expiry=%s cache_age_seconds=%.1f contracts=%s",
            expected_exchange,
            expected_underlying,
            expected_expiry.isoformat(),
            (market_now_naive - latest_time).total_seconds(),
            len(contracts),
        )

        frame = frames_by_symbol.get(underlying)
        spot_price = 0.0
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            spot_price = float(frame.iloc[-1]["close"])
        strikes = sorted({contract.strike for contract in contracts})
        if spot_price <= 0:
            spot_price = float(strikes[len(strikes) // 2])
        atm_strike = int(round(min(strikes, key=lambda strike: abs(strike - spot_price))))
        return OptionChainSnapshot(
            underlying=underlying,
            spot_price=spot_price,
            atm_strike=atm_strike,
            expiry_date=latest.expiry_date,
            snapshot_time=latest_time,
            contracts=contracts,
            requested_expiry=latest.expiry_date,
            fallback_used=True,
        )

    def _with_oi_change_percent(
        self,
        snapshot: OptionChainSnapshot,
        previous_option_map: dict[tuple[float, str], OptionOISnapshot],
    ) -> OptionChainSnapshot:
        if not previous_option_map:
            return snapshot

        contracts: list[OptionContract] = []
        changed_count = 0
        for contract in snapshot.contracts:
            key = (float(contract.strike), str(contract.option_type).strip().upper())
            previous = previous_option_map.get(key)
            if previous is None:
                contracts.append(contract)
                continue

            previous_oi = float(previous.oi or 0.0)
            current_oi = float(contract.oi or 0.0)
            if previous_oi <= 0:
                contracts.append(contract)
                continue

            oi_change_pct = ((current_oi - previous_oi) / previous_oi) * 100.0
            contracts.append(replace(contract, oi_change=oi_change_pct))
            changed_count += 1

        if changed_count:
            logger.info(
                "Option OI change normalized from previous snapshot underlying=%s expiry=%s contracts=%s",
                snapshot.underlying,
                snapshot.expiry_date.isoformat(),
                changed_count,
            )
        return replace(snapshot, contracts=tuple(contracts))

    def _store_option_chain(self, *, db: Session, run_id: int, snapshot: OptionChainSnapshot) -> None:
        snapshot_time = self._to_db_time(snapshot.snapshot_time)

        for contract in snapshot.contracts:
            oi_value = float(contract.oi)
            if oi_value < 0:
                logger.warning(
                    "Negative option OI received for %s %s %s; storing as 0.",
                    snapshot.underlying,
                    contract.strike,
                    contract.option_type,
                )
                oi_value = 0.0

            db.add(
                OptionOISnapshot(
                    run_id=run_id,
                    underlying=snapshot.underlying,
                    security_id=contract.security_id,
                    expiry_date=snapshot.expiry_date,
                    strike_price=float(contract.strike),
                    option_type=contract.option_type,
                    ltp=float(contract.ltp),
                    oi=oi_value,
                    oi_change=float(contract.oi_change),
                    traded_volume=float(contract.volume),
                    snapshot_time=snapshot_time,
                )
            )

    @staticmethod
    def _compact_ohlc_log(frame: pd.DataFrame) -> list[dict[str, float | str]]:
        tail = frame[["open", "high", "low", "close", "volume"]].tail(3).copy()
        output: list[dict[str, float | str]] = []
        for timestamp, row in tail.iterrows():
            ts = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
            output.append(
                {
                    "time": ts,
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "volume": round(float(row["volume"]), 2),
                }
            )
        return output

    def _to_db_time(self, value: datetime | pd.Timestamp) -> datetime:
        if isinstance(value, pd.Timestamp):
            dt = value.to_pydatetime()
        else:
            dt = value

        return to_ist_naive(dt)





