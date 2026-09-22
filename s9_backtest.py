from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.config import SYMBOL_CONFIG, get_settings, normalize_market_symbol
from backend.app.services.dhan_client import DhanClient
from backend.app.services.option_types import OptionChainSnapshot, OptionContract
from backend.app.services.screener_engine import ScreenerEngine
from backend.app.utils.timeframe import normalize_ohlcv_frame
from backtesting import (
    _build_state_series,
    _ensure_tz,
    _state_at_or_before,
)


@dataclass(frozen=True)
class S9Trade:
    entry_time: datetime
    exit_time: datetime
    signal: str
    option_symbol: str
    option_type: str
    strike: int
    entry_price: float
    exit_price: float

    @property
    def pnl_points(self) -> float:
        return self.exit_price - self.entry_price

    @property
    def pnl_pct(self) -> float:
        if abs(self.entry_price) < 1e-9:
            return 0.0
        return (self.pnl_points / self.entry_price) * 100.0


# Index symbols that should use OPTIDX
INDEX_SYMBOLS = {"NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "SENSEX"}


def get_instrument_type(symbol: str) -> str:
    """Determine instrument type based on symbol."""
    canonical = normalize_market_symbol(symbol)
    config = SYMBOL_CONFIG.get(canonical)
    if config and config.get("instrument_type") == "index":
        return "OPTIDX"
    return "OPTSTK"


def get_underlying_info(symbol: str) -> tuple[str, str]:
    """Get exchange segment and instrument for underlying cash candles."""
    symbol_upper = symbol.upper().strip()
    symbol_clean = symbol_upper.replace(" ", "")
    
    if symbol_clean in INDEX_SYMBOLS:
        # For indices, we need the index security ID from config
        return "NSE_INDEX", "INDEX"
    else:
        # For stocks
        return "NSE_EQ", "EQUITY"


FILTER_KEYS = ("sweep", "delta", "theta", "pcr", "pcr_shift", "vwap", "order_book")


def filter_passed(payload: dict[str, Any], key: str) -> bool:
    filters = payload.get("filters")
    if not isinstance(filters, dict):
        return False
    item = filters.get(key)
    return bool(item.get("passed")) if isinstance(item, dict) else False


def filter_reason(payload: dict[str, Any], key: str) -> str:
    filters = payload.get("filters")
    if not isinstance(filters, dict):
        return ""
    item = filters.get(key)
    if not isinstance(item, dict):
        return ""
    return str(item.get("reason") or "")


def parse_market_clock(value: str, fallback: time) -> time:
    try:
        hours, minutes = str(value or "").split(":", 1)
        return time(int(hours), int(minutes))
    except (TypeError, ValueError):
        return fallback


def market_session_ranges(start: datetime, end: datetime, *, settings, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    start_tz = _ensure_tz(start, tz)
    end_tz = _ensure_tz(end, tz)
    market_open = parse_market_clock(settings.market_open_time, time(9, 15))
    market_close = parse_market_clock(settings.market_close_time, time(15, 30))
    ranges: list[tuple[datetime, datetime]] = []
    day = start_tz.date()
    while day <= end_tz.date():
        if day.weekday() < 5:
            session_start = datetime.combine(day, market_open, tzinfo=tz)
            session_end = datetime.combine(day, market_close, tzinfo=tz)
            chunk_start = max(start_tz, session_start)
            chunk_end = min(end_tz, session_end)
            if chunk_start < chunk_end:
                ranges.append((chunk_start, chunk_end))
        day = day + timedelta(days=1)
    return ranges


def fetch_intraday_multi_session(
    dhan: DhanClient,
    *,
    security_id: str,
    exchange_segment: str,
    instrument: str,
    from_market: datetime,
    to_market: datetime,
    interval_minutes: int,
    include_oi: bool,
    trading_symbol: str | None,
    settings,
    tz: ZoneInfo,
    verbose: bool,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    sessions = market_session_ranges(from_market, to_market, settings=settings, tz=tz)
    if verbose:
        print(f"Fetching {len(sessions)} market sessions for {trading_symbol or security_id}")
    for session_start, session_end in sessions:
        try:
            frame = dhan.get_intraday_ohlc(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument=instrument,
                from_date=session_start.replace(tzinfo=None),
                to_date=session_end.replace(tzinfo=None),
                interval_minutes=interval_minutes,
                include_oi=include_oi,
                trading_symbol=trading_symbol,
            )
        except RuntimeError as exc:
            if verbose:
                print(f"{session_start.date()} fetch failed security_id={security_id}: {exc}")
            continue
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined


def resolve_instrument(
    scrip_master: pd.DataFrame,
    symbol: str,
    expiry_date,
    strike: float,
    opt_type: str,
    verbose: bool = False,
    exchange: str = "NSE",
    option_prefix: str | None = None,
) -> tuple[str, str]:
    """Resolve security ID and instrument type for a given symbol and contract."""
    symbol_upper = symbol.upper().strip()
    symbol_clean = symbol_upper.replace(" ", "")
    
    # Determine instrument type
    instrument_type = get_instrument_type(symbol_clean)
    prefix = str(option_prefix or symbol_clean).upper().replace(" ", "")
    
    if verbose:
        print(f"Resolving {symbol_clean} with instrument {instrument_type}")
    
    # Filter scrip master
    filtered = scrip_master[
        (scrip_master["SEM_EXM_EXCH_ID"] == exchange.upper()) &
        (scrip_master["SEM_INSTRUMENT_NAME"] == instrument_type) &
        (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.replace(" ", "").str.startswith(f"{prefix}-"))
    ].copy()
    
    if filtered.empty:
        # Try with more flexible matching
        filtered = scrip_master[
            (scrip_master["SEM_EXM_EXCH_ID"] == exchange.upper()) &
            (scrip_master["SEM_INSTRUMENT_NAME"] == instrument_type) &
            (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.replace(" ", "").str.startswith(prefix))
        ].copy()
    
    if filtered.empty:
        raise RuntimeError(
            f"No NSE {instrument_type} rows found for {symbol}.\n"
            f"Symbol: {symbol}\n"
            f"Instrument type: {instrument_type}\n"
            f"Available symbols in scrip master: {scrip_master[scrip_master['SEM_INSTRUMENT_NAME'] == instrument_type]['SEM_TRADING_SYMBOL'].unique()[:10].tolist()}"
        )
    
    # Clean data
    filtered["SEM_EXPIRY_DATE"] = pd.to_datetime(filtered["SEM_EXPIRY_DATE"], errors="coerce")
    filtered["SEM_STRIKE_PRICE"] = pd.to_numeric(filtered["SEM_STRIKE_PRICE"], errors="coerce")
    filtered["SEM_SMST_SECURITY_ID"] = filtered["SEM_SMST_SECURITY_ID"].astype(str)
    filtered["SEM_OPTION_TYPE"] = filtered["SEM_OPTION_TYPE"].astype(str).str.upper()
    filtered["EXPIRY_DATE_ONLY"] = filtered["SEM_EXPIRY_DATE"].dt.date
    
    # Remove invalid rows
    filtered = filtered.dropna(subset=["SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE"])
    
    if filtered.empty:
        raise RuntimeError(f"No valid contracts found for {symbol} after cleaning.")
    
    # Convert expiry to date if it's a datetime
    if hasattr(expiry_date, 'date'):
        expiry_date = expiry_date.date()
    elif isinstance(expiry_date, pd.Timestamp):
        expiry_date = expiry_date.date()
    
    # Find matching contract
    subset = filtered[
        (filtered["SEM_OPTION_TYPE"] == opt_type) &
        (filtered["SEM_STRIKE_PRICE"] == float(strike)) &
        (filtered["EXPIRY_DATE_ONLY"] == expiry_date)
    ]
    
    if subset.empty:
        # Try with nearby strikes
        strike_tolerance = 5  # 5 points tolerance
        subset = filtered[
            (filtered["SEM_OPTION_TYPE"] == opt_type) &
            (filtered["SEM_STRIKE_PRICE"].between(float(strike) - strike_tolerance, float(strike) + strike_tolerance)) &
            (filtered["EXPIRY_DATE_ONLY"] == expiry_date)
        ]
        
        if subset.empty:
            # Try nearest expiry
            expiries = sorted(filtered["EXPIRY_DATE_ONLY"].unique())
            for exp in expiries:
                if exp >= expiry_date:
                    subset = filtered[
                        (filtered["SEM_OPTION_TYPE"] == opt_type) &
                        (filtered["SEM_STRIKE_PRICE"].between(float(strike) - strike_tolerance, float(strike) + strike_tolerance)) &
                        (filtered["EXPIRY_DATE_ONLY"] == exp)
                    ]
                    if not subset.empty:
                        if verbose:
                            print(f"Using nearest expiry {exp} instead of {expiry_date}")
                        break
    
    if subset.empty:
        # Debug info
        available_strikes = filtered[
            (filtered["SEM_OPTION_TYPE"] == opt_type) &
            (filtered["EXPIRY_DATE_ONLY"] == expiry_date)
        ]["SEM_STRIKE_PRICE"].unique()[:10].tolist()
        
        raise RuntimeError(
            f"Unable to resolve {symbol} {expiry_date} strike {strike} {opt_type}.\n"
            f"Instrument type: {instrument_type}\n"
            f"Available strikes for this expiry: {available_strikes}\n"
            f"Available expiries: {sorted(filtered['EXPIRY_DATE_ONLY'].unique())[:5]}"
        )
    
    security_id = str(subset.iloc[0]["SEM_SMST_SECURITY_ID"])
    
    if verbose:
        print(f"Resolved {symbol} {expiry_date} {strike} {opt_type} -> security_id {security_id}")
    
    return security_id, instrument_type


def run_s9_backtest(
    *,
    days: int,
    hold_minutes: int,
    out_csv: Path | None,
    trades_csv: Path | None,
    verbose: bool,
    symbol_override: str = "",
    chain_depth: int | None = None,
    max_candles: int = 0,
    min_entry_score: float = 1.0,
) -> dict[str, Any]:
    settings = get_settings()
    if str(symbol_override or "").strip():
        override_symbol = normalize_market_symbol(symbol_override)
        settings = settings.model_copy(
            update={
                "underlying_symbol": override_symbol,
                "nifty_index_symbol": override_symbol,
                "groww_trading_symbol": override_symbol,
                "groww_underlying_symbol": override_symbol,
            }
        )
    if chain_depth is not None:
        settings.dhan.option_chain_depth = max(0, int(chain_depth))
    tz = ZoneInfo(settings.market_timezone)
    now_market = datetime.now(tz)
    from_market = now_market - timedelta(days=int(days))

    # Check if Dhan is configured
    dhan = DhanClient(settings)
    if not dhan.configured:
        raise RuntimeError("Dhan API not configured. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")

    # Get symbol from settings or use default
    symbol = normalize_market_symbol(settings.underlying_symbol or settings.nifty_index_symbol)
    symbol_clean = symbol.replace(" ", "")
    symbol_config = SYMBOL_CONFIG.get(symbol, {})
    option_prefix = str(symbol_config.get("option_underlying") or symbol_clean).upper().replace(" ", "")
    option_exchange = str(symbol_config.get("option_exchange") or "NSE").upper()
    
    # Determine if this is an index or stock
    is_index = get_instrument_type(symbol) == "OPTIDX"
    instrument_type = get_instrument_type(symbol_clean)
    option_instrument = instrument_type
    
    print(f"Symbol: {symbol}")
    print(f"Symbol clean: {symbol_clean}")
    print(f"Is index: {is_index}")
    print(f"Instrument type: {instrument_type}")

    # Scrip master path
    scrip_master_path = Path(__file__).resolve().parent / "backend" / "app" / ".cache" / "dhan_api_scrip_master.csv"
    if not scrip_master_path.exists():
        raise RuntimeError(f"Missing Dhan scrip master at {scrip_master_path}.")

    if verbose:
        print(f"Loading scrip master: {scrip_master_path}")

    # Load scrip master
    scrip_master = pd.read_csv(
        scrip_master_path,
        usecols=[
            "SEM_EXM_EXCH_ID",
            "SEM_INSTRUMENT_NAME",
            "SEM_SMST_SECURITY_ID",
            "SEM_TRADING_SYMBOL",
            "SEM_EXPIRY_DATE",
            "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE",
        ],
        low_memory=False,
    )
    
    # Inspect scrip master columns and sample data
    if verbose:
        print("\nScrip master columns:")
        print(scrip_master.columns.tolist())
        print("\nSample of scrip master:")
        print(scrip_master.head(3))
        
        # Check for DIXON specifically
        dixon_rows = scrip_master[scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.contains("DIXON", case=False, na=False)]
        if not dixon_rows.empty:
            print(f"\nFound {len(dixon_rows)} rows for DIXON:")
            print(dixon_rows[["SEM_TRADING_SYMBOL", "SEM_INSTRUMENT_NAME", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE"]].head(5))
        else:
            print("\nNo rows found for DIXON in scrip master.")
            # Show what instrument types are available
            print("\nAvailable instrument types:")
            print(scrip_master["SEM_INSTRUMENT_NAME"].unique())
    
    # For index, filter for option contracts
    if is_index:
        # Filter scrip master for index options
        scrip_master_filtered = scrip_master[
            (scrip_master["SEM_EXM_EXCH_ID"] == option_exchange) &
            (scrip_master["SEM_INSTRUMENT_NAME"] == "OPTIDX") &
            (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.replace(" ", "").str.startswith(f"{option_prefix}-"))
        ].copy()
        
        if scrip_master_filtered.empty:
            raise RuntimeError(
                f"No {option_exchange} OPTIDX {symbol} rows found in scrip master.\n"
                f"Available symbols with OPTIDX: {scrip_master[scrip_master['SEM_INSTRUMENT_NAME'] == 'OPTIDX']['SEM_TRADING_SYMBOL'].unique()[:10].tolist()}"
            )
        
        scrip_master_options = scrip_master_filtered
    else:
        # For stock, filter for stock options (OPTSTK)
        scrip_master_filtered = scrip_master[
            (scrip_master["SEM_EXM_EXCH_ID"] == "NSE") &
            (scrip_master["SEM_INSTRUMENT_NAME"] == "OPTSTK") &
            (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.replace(" ", "").str.startswith(symbol_clean))
        ].copy()
        
        if scrip_master_filtered.empty:
            # Try with contains instead of startswith
            scrip_master_filtered = scrip_master[
                (scrip_master["SEM_EXM_EXCH_ID"] == "NSE") &
                (scrip_master["SEM_INSTRUMENT_NAME"] == "OPTSTK") &
                (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.contains(symbol_clean))
            ].copy()
        
        if scrip_master_filtered.empty:
            raise RuntimeError(
                f"No NSE OPTSTK {symbol} rows found in scrip master.\n"
                f"Available symbols with OPTSTK: {scrip_master[scrip_master['SEM_INSTRUMENT_NAME'] == 'OPTSTK']['SEM_TRADING_SYMBOL'].unique()[:10].tolist()}"
            )
        
        scrip_master_options = scrip_master_filtered
    
    if verbose:
        print(f"Found {len(scrip_master_options)} option rows for {symbol}")

    # Clean scrip master data
    scrip_master_options["SEM_EXPIRY_DATE"] = pd.to_datetime(scrip_master_options["SEM_EXPIRY_DATE"], errors="coerce")
    scrip_master_options["SEM_STRIKE_PRICE"] = pd.to_numeric(scrip_master_options["SEM_STRIKE_PRICE"], errors="coerce")
    scrip_master_options["SEM_SMST_SECURITY_ID"] = scrip_master_options["SEM_SMST_SECURITY_ID"].astype(str)
    scrip_master_options["SEM_OPTION_TYPE"] = scrip_master_options["SEM_OPTION_TYPE"].astype(str).str.upper()
    scrip_master_options = scrip_master_options.dropna(subset=["SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE"])
    scrip_master_options["EXPIRY_DATE_ONLY"] = scrip_master_options["SEM_EXPIRY_DATE"].dt.date

    # GET UNDERLYING DATA
    try:
        if verbose:
            print(f"Fetching underlying data for {symbol}...")
            print(f"Date range: {from_market} to {now_market}")
        
        # Get underlying security ID
        if is_index:
            security_id = settings.dhan.nifty_security_id
            security_id = settings.nifty50_security_map.get(symbol) or security_id
            exchange_segment = settings.dhan.nifty_exchange_segment
            instrument = settings.dhan.nifty_instrument
        else:
            # For stocks, we need to find the equity security ID
            equity_rows = scrip_master[
                (scrip_master["SEM_EXM_EXCH_ID"] == "NSE") &
                (scrip_master["SEM_INSTRUMENT_NAME"] == "EQUITY") &
                (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.replace(" ", "").str.startswith(symbol_clean))
            ]
            
            if equity_rows.empty:
                raise RuntimeError(f"No EQUITY rows found for {symbol} in scrip master.")
            
            security_id = str(equity_rows.iloc[0]["SEM_SMST_SECURITY_ID"])
            exchange_segment = "NSE_EQ"
            instrument = "EQUITY"
        
        if verbose:
            print(f"Underlying security_id: {security_id}")
            print(f"Exchange segment: {exchange_segment}")
            print(f"Instrument: {instrument}")
        
        underlying_1m = fetch_intraday_multi_session(
            dhan,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument=instrument,
            from_market=from_market,
            to_market=now_market,
            interval_minutes=1,
            include_oi=False,
            trading_symbol=symbol,
            settings=settings,
            tz=tz,
            verbose=verbose,
        )
        
        if underlying_1m is None or underlying_1m.empty:
            raise RuntimeError(f"No underlying data received from Dhan API for {symbol}.")
        
    except Exception as e:
        raise RuntimeError(f"Error fetching underlying data for {symbol}: {e}")

    if underlying_1m is None or underlying_1m.empty:
        raise RuntimeError(f"No underlying data available for {symbol}. Cannot run backtest.")

    if verbose:
        print(f"Got {len(underlying_1m)} candles for underlying")

    # Build state series
    index_indicator_by_tf = {}
    for tf in settings.timeframes:
        try:
            state_series = _build_state_series(
                symbol=symbol,
                raw_1m=underlying_1m,
                timeframe=tf,
                market_timezone=settings.market_timezone,
                market_open_time=settings.market_open_time,
                now_market=now_market,
            )
            if state_series is not None and not state_series.empty:
                index_indicator_by_tf[tf] = state_series
        except Exception as e:
            if verbose:
                print(f"Warning: Could not build state series for {tf}: {e}")
    
    if not index_indicator_by_tf:
        raise RuntimeError("No state series could be built. Check data format.")

    base_frame = index_indicator_by_tf.get("5m", pd.DataFrame())
    if base_frame.empty:
        # Try to use any available timeframe
        for tf, frame in index_indicator_by_tf.items():
            if not frame.empty:
                base_frame = frame
                if verbose:
                    print(f"Using {tf} timeframe as base instead of 5m")
                break
    
    if base_frame.empty:
        raise RuntimeError(f"No {symbol} data available.")

    if verbose:
        print(f"Base frame has {len(base_frame)} rows")

    def nearest_available_strike(*, price: float, expiry) -> int:
        rows = scrip_master_options[
            (scrip_master_options["EXPIRY_DATE_ONLY"] == expiry)
            & (scrip_master_options["SEM_OPTION_TYPE"].isin(["CE", "PE"]))
        ]
        strikes = sorted({float(row) for row in rows["SEM_STRIKE_PRICE"].dropna().tolist()})
        if not strikes:
            return atm_strike_for_spot(price)
        return int(min(strikes, key=lambda strike: (abs(strike - float(price)), strike)))

    def atm_strike_for_spot(price: float) -> int:
        if is_index:
            return int(round(price / 100.0) * 100)
        else:
            # For stocks, use 50-point intervals
            return int(round(price / 50.0) * 50)

    def pick_expiry(at_time: pd.Timestamp):
        at_date = pd.Timestamp(at_time).date()
        future = scrip_master_options.loc[scrip_master_options["EXPIRY_DATE_ONLY"] >= at_date, "EXPIRY_DATE_ONLY"]
        if future.empty:
            return max(scrip_master_options["EXPIRY_DATE_ONLY"])
        return min(future)

    def resolve_security_id(*, expiry, strike: int, opt_type: str) -> str:
        security_id, _ = resolve_instrument(
            scrip_master=scrip_master,
            symbol=symbol,
            expiry_date=expiry,
            strike=float(strike),
            opt_type=opt_type,
            verbose=verbose,
            exchange=option_exchange,
            option_prefix=option_prefix,
        )
        return security_id

    def strike_window(atm_strike: int, expiry=None) -> list[int]:
        depth = max(0, int(settings.dhan.option_chain_depth))
        if not is_index and expiry is not None:
            rows = scrip_master_options[
                (scrip_master_options["EXPIRY_DATE_ONLY"] == expiry)
                & (scrip_master_options["SEM_OPTION_TYPE"].isin(["CE", "PE"]))
            ]
            strikes = sorted({int(float(row)) for row in rows["SEM_STRIKE_PRICE"].dropna().tolist()})
            if atm_strike in strikes:
                center = strikes.index(atm_strike)
                return strikes[max(0, center - depth): center + depth + 1]
        if symbol_clean == "NIFTY50" or symbol_clean == "FINNIFTY":
            interval = 50
        elif is_index:
            interval = 100
        else:
            interval = 50  # For stocks
        
        return [atm_strike + (offset * interval) for offset in range(-depth, depth + 1)]

    option_1m_cache: dict[str, pd.DataFrame] = {}
    option_5m_cache: dict[str, pd.DataFrame] = {}

    def get_option_5m(security_id: str) -> pd.DataFrame:
        cached = option_5m_cache.get(security_id)
        if cached is not None:
            return cached
        raw = option_1m_cache.get(security_id)
        if raw is None:
            try:
                raw = fetch_intraday_multi_session(
                    dhan,
                    security_id=security_id,
                    exchange_segment=settings.dhan.options_exchange_segment,
                    instrument=option_instrument,
                    from_market=from_market,
                    to_market=now_market,
                    interval_minutes=1,
                    include_oi=True,
                    trading_symbol=None,
                    settings=settings,
                    tz=tz,
                    verbose=verbose,
                )
            except RuntimeError as exc:
                if verbose:
                    print(f"option fetch failed security_id={security_id}: {exc}")
                raw = pd.DataFrame()
            option_1m_cache[security_id] = raw
        if raw.empty:
            option_5m_cache[security_id] = raw
            return raw
        
        try:
            normalized = normalize_ohlcv_frame(raw, settings.market_timezone)
            frame = (
                normalized.resample(
                    "5min",
                    origin="start_day",
                    offset=pd.Timedelta(hours=9, minutes=15),
                    closed="right",
                    label="right",
                )
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                        "oi": "last",
                    }
                )
                .dropna(subset=["open", "high", "low", "close"])
            )
            frame = frame.loc[frame.index <= now_market]
            option_5m_cache[security_id] = frame
            return frame
        except Exception as e:
            if verbose:
                print(f"Error processing option data for {security_id}: {e}")
            option_5m_cache[security_id] = raw
            return raw

    def contract_at(*, strike: int, opt_type: str, security_id: str, frame: pd.DataFrame, when: pd.Timestamp) -> OptionContract | None:
        if frame.empty:
            return None
        view = frame.loc[:when]
        if view.empty:
            return None
        row = view.iloc[-1]
        oi = float(row.get("oi", 0.0) or 0.0)
        prev_oi = float(view.iloc[-2].get("oi", 0.0) or 0.0) if len(view) >= 2 else oi
        return OptionContract(
            security_id=security_id,
            strike=float(strike),
            option_type=opt_type,
            ltp=float(row.get("close", 0.0) or 0.0),
            oi=oi,
            oi_change=oi - prev_oi,
            volume=float(row.get("volume", 0.0) or 0.0),
        )

    def option_chain_for_window(*, expiry, atm_strike: int, spot_price: float, when: pd.Timestamp) -> tuple[OptionChainSnapshot | None, dict[tuple[int, str], str]]:
        contracts: list[OptionContract] = []
        security_ids: dict[tuple[int, str], str] = {}
        for strike in strike_window(atm_strike, expiry):
            for opt_type in ("CE", "PE"):
                try:
                    security_id = resolve_security_id(expiry=expiry, strike=strike, opt_type=opt_type)
                except RuntimeError as exc:
                    if verbose:
                        print(f"{when.isoformat()} skip strike={strike} {opt_type}: {exc}")
                    continue
                frame = get_option_5m(security_id)
                contract = contract_at(strike=strike, opt_type=opt_type, security_id=security_id, frame=frame, when=when)
                if contract is not None:
                    contracts.append(contract)
                    security_ids[(strike, opt_type)] = security_id
        if not contracts:
            return None, security_ids
        snap_time = when.to_pydatetime() if isinstance(when, pd.Timestamp) else pd.Timestamp(when).to_pydatetime()
        return (
            OptionChainSnapshot(
                underlying=symbol,
                spot_price=float(spot_price),
                atm_strike=int(atm_strike),
                expiry_date=expiry,
                snapshot_time=snap_time,
                contracts=tuple(sorted(contracts, key=lambda row: (row.strike, row.option_type))),
            ),
            security_ids,
        )

    engine = ScreenerEngine(settings)

    signal_rows: list[dict[str, Any]] = []
    trades: list[S9Trade] = []
    open_trade: dict[str, Any] | None = None
    hold_delta = timedelta(minutes=int(hold_minutes))

    # Process each candle
    processed_candles = 0
    for idx in range(1, len(base_frame)):
        if max_candles and processed_candles >= int(max_candles):
            break
        try:
            when = pd.Timestamp(base_frame.index[idx])
            spot_price = float(base_frame.iloc[idx].get("close", 0.0) or 0.0)
            expiry = pick_expiry(when)
            atm_strike = nearest_available_strike(price=spot_price, expiry=expiry) if not is_index else atm_strike_for_spot(spot_price)

            states_by_timeframe = {}
            missing_state = False
            for tf in settings.timeframes:
                if tf in index_indicator_by_tf:
                    idx_state = _state_at_or_before(index_indicator_by_tf[tf], symbol=symbol, timeframe=tf, when=when)
                    if idx_state is None:
                        missing_state = True
                        break
                    states_by_timeframe[tf] = {symbol: idx_state}
            
            if missing_state:
                continue
            processed_candles += 1

            option_chain, security_ids = option_chain_for_window(
                expiry=expiry,
                atm_strike=atm_strike,
                spot_price=spot_price,
                when=when,
            )
            if option_chain is None:
                continue

            # Run S9 directly from the current state on every candle.
            try:
                s9_results = engine._run_s9(
                    states_by_timeframe=states_by_timeframe,
                    s1_signals=[],
                    option_chain=option_chain,
                    now_market=when.to_pydatetime(),
                )
                if not s9_results:
                    continue
                s9 = s9_results[0]
                payload = s9.payload if isinstance(s9.payload, dict) else {}
            except Exception as e:
                if verbose:
                    print(f"S9 execution error at {when}: {e}")
                continue

            passed_count = int(payload.get("passed_count") or 0)
            total_filters = int(payload.get("total_filters") or 0)
            score = float(payload.get("score") or (passed_count / total_filters if total_filters else 0.0))
            row = {
                "time": when.isoformat(),
                "symbol": symbol,
                "spot_price": spot_price,
                "atm_strike": atm_strike,
                "expiry": str(expiry),
                "signal": s9.signal,
                "option_type": payload.get("option_type"),
                "strike": payload.get("strike"),
                "option_symbol": payload.get("option_symbol"),
                "passed_count": passed_count,
                "total_filters": total_filters,
                "score": score,
                "confirmed": bool(payload.get("confirmed")),
                "rejection_reason": payload.get("rejection_reason"),
                "scanned_contract_count": payload.get("scanned_contract_count"),
                "fully_matched_contract_count": payload.get("fully_matched_contract_count"),
            }
            for key in FILTER_KEYS:
                row[f"{key}_pass"] = "PASS" if filter_passed(payload, key) else "FAIL"
                row[f"{key}_reason"] = filter_reason(payload, key)
            signal_rows.append(row)

            option_type = str(payload.get("option_type") or "")
            selected_strike = int(payload.get("selected_strike") or payload.get("strike") or 0)
            selected_security_id = security_ids.get((selected_strike, option_type))
            option_frame = get_option_5m(selected_security_id) if selected_security_id else pd.DataFrame()
            option_view = option_frame.loc[:when] if not option_frame.empty else pd.DataFrame()
            option_price = float(option_view.iloc[-1].get("close", 0.0) or 0.0) if not option_view.empty else 0.0

            entry_allowed = score >= float(min_entry_score) and option_price > 0 and selected_security_id
            if entry_allowed and open_trade is None:
                open_trade = {
                    "entry_time": when.to_pydatetime(),
                    "signal": str(s9.signal),
                    "option_symbol": str(payload.get("option_symbol") or ""),
                    "option_type": option_type,
                    "strike": selected_strike or atm_strike,
                    "entry_price": option_price,
                    "security_id": selected_security_id,
                }

            if open_trade is not None and when.to_pydatetime() >= open_trade["entry_time"] + hold_delta:
                exit_frame = get_option_5m(str(open_trade["security_id"])) if open_trade["security_id"] else pd.DataFrame()
                exit_view = exit_frame.loc[:when] if not exit_frame.empty else pd.DataFrame()
                if not exit_view.empty:
                    trades.append(
                        S9Trade(
                            entry_time=open_trade["entry_time"],
                            exit_time=when.to_pydatetime(),
                            signal=open_trade["signal"],
                            option_symbol=open_trade["option_symbol"],
                            option_type=open_trade["option_type"],
                            strike=int(open_trade["strike"]),
                            entry_price=float(open_trade["entry_price"]),
                            exit_price=float(exit_view.iloc[-1].get("close", 0.0) or 0.0),
                        )
                    )
                open_trade = None

        except Exception as e:
            if verbose:
                print(f"Error processing candle {idx}: {e}")
            continue

    # Save outputs
    if out_csv is not None and signal_rows:
        output_df = pd.DataFrame(signal_rows)
        output_df.to_csv(out_csv, index=False)
    
    if trades_csv is not None and trades:
        pd.DataFrame([trade.__dict__ | {"pnl_points": trade.pnl_points, "pnl_pct": trade.pnl_pct} for trade in trades]).to_csv(
            trades_csv,
            index=False,
        )

    # Calculate statistics
    signal_df = pd.DataFrame(signal_rows)
    signal_counts = (
        signal_df["signal"].fillna("None").value_counts().to_dict()
        if not signal_df.empty and "signal" in signal_df
        else {}
    )
    rejection_counts = (
        signal_df["rejection_reason"].fillna("CONFIRMED").value_counts().to_dict()
        if not signal_df.empty and "rejection_reason" in signal_df
        else {}
    )
    entry_df = (
        signal_df[signal_df["score"] >= float(min_entry_score)]
        if not signal_df.empty and "score" in signal_df
        else pd.DataFrame()
    )

    pnl_points = [trade.pnl_points for trade in trades]
    pnl_pct = [trade.pnl_pct for trade in trades]
    pnl_summary = {"trades": 0}
    if trades:
        pnl_summary = {
            "trades": len(trades),
            "win_rate_pct": round(100.0 * sum(1 for pnl in pnl_points if pnl > 0) / len(trades), 2),
            "avg_pnl_points": round(sum(pnl_points) / len(trades), 4),
            "avg_pnl_pct": round(sum(pnl_pct) / len(trades), 4),
            "total_pnl_points": round(sum(pnl_points), 4),
            "total_pnl_pct": round(sum(pnl_pct), 4),
        }

    return {
        "params": {
            "days": days,
            "hold_minutes": hold_minutes,
            "min_entry_score": min_entry_score,
            "symbol": symbol,
            "is_index": is_index,
            "instrument_type": instrument_type,
            "candle": "5m",
            "processed_candles": processed_candles,
            "chain_depth": settings.dhan.option_chain_depth,
        },
        "signals": len(signal_rows),
        "entry_signals": len(entry_df),
        "signal_counts": signal_counts,
        "rejection_counts": rejection_counts,
        "latest_entries": entry_df.tail(10).to_dict("records") if not entry_df.empty else [],
        "pnl_summary": pnl_summary,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S9 5m filter-score backtest for configured stocks and indices.")
    parser.add_argument("--days", type=int, default=5, help="How many calendar days to fetch.")
    parser.add_argument("--hold-minutes", type=int, default=30, help="Fixed option hold duration.")
    parser.add_argument("--out", default="s9_signals.csv", help="Signal stream CSV path.")
    parser.add_argument("--trades-out", default="", help="Optional trades CSV path.")
    parser.add_argument("--symbol", default="", help="Symbol, comma-separated symbols, or ALL for every configured stock/index.")
    parser.add_argument("--chain-depth", type=int, default=-1, help="Override option-chain depth around ATM for faster backtests.")
    parser.add_argument("--max-candles", type=int, default=0, help="Stop after this many eligible candles (0 = all).")
    parser.add_argument("--min-entry-score", type=float, default=1.0, help="Minimum score for simulated entry. 1.0 means all filters pass.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        raw_symbols = str(args.symbol or "").strip()
        if raw_symbols.upper() == "ALL":
            symbols = list(SYMBOL_CONFIG)
        elif "," in raw_symbols:
            symbols = [normalize_market_symbol(item) for item in raw_symbols.split(",") if item.strip()]
        else:
            symbols = [normalize_market_symbol(raw_symbols)] if raw_symbols else [""]

        def scoped_path(raw_path: str, symbol: str) -> Path | None:
            if not raw_path:
                return None
            path = Path(raw_path).resolve()
            if len(symbols) <= 1:
                return path
            safe_symbol = normalize_market_symbol(symbol).replace(" ", "")
            return path.with_name(f"{path.stem}_{safe_symbol}{path.suffix}")

        results = {}
        for symbol in symbols:
            results[symbol or "DEFAULT"] = run_s9_backtest(
                days=args.days,
                hold_minutes=args.hold_minutes,
                out_csv=scoped_path(args.out, symbol) if args.out else None,
                trades_csv=scoped_path(args.trades_out, symbol) if args.trades_out else None,
                verbose=bool(args.verbose),
                symbol_override=str(symbol or ""),
                chain_depth=args.chain_depth if args.chain_depth >= 0 else None,
                max_candles=int(args.max_candles or 0),
                min_entry_score=float(args.min_entry_score),
            )
        result = results[symbols[0] or "DEFAULT"] if len(results) == 1 else results
        print("Backtest completed successfully!")
        print(f"Results: {result}")
        return 0
    except Exception as e:
        print(f"Error running backtest: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
