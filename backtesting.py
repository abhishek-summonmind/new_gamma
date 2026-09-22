from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import io
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.config import get_settings
from backend.app.services.dhan_client import DhanClient, OptionChainSnapshot, OptionContract
from backend.app.services.indicator_engine import SymbolIndicatorState
from backend.app.services.screener_engine import ScreenerEngine
from backend.app.utils.indicators import IndicatorPoint, build_indicator_frame
from backend.app.utils.timeframe import resample_ohlcv


@dataclass(frozen=True)
class Trade:
    entry_time: datetime
    exit_time: datetime
    signal: str
    entry_price: float
    exit_price: float

    @property
    def pnl_points(self) -> float:
        direction = 1.0 if self.signal in {"buy", "strong_buy"} else -1.0
        return direction * (self.exit_price - self.entry_price)

    @property
    def pnl_pct(self) -> float:
        if abs(self.entry_price) < 1e-9:
            return 0.0
        return (self.pnl_points / self.entry_price) * 100.0


def _ensure_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _row_to_point(ts: pd.Timestamp, row: pd.Series) -> IndicatorPoint:
    candle_time = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else pd.Timestamp(ts).to_pydatetime()
    return IndicatorPoint(
        candle_time=candle_time,
        open=float(row.get("open", 0.0)),
        high=float(row.get("high", 0.0)),
        low=float(row.get("low", 0.0)),
        close=float(row.get("close", 0.0)),
        volume=float(row.get("volume", 0.0)),
        volume_ma=None if pd.isna(row.get("volume_ma")) else float(row.get("volume_ma")),
        rsi=None if pd.isna(row.get("rsi")) else float(row.get("rsi")),
        macd=None if pd.isna(row.get("macd")) else float(row.get("macd")),
        macd_signal=None if pd.isna(row.get("macd_signal")) else float(row.get("macd_signal")),
        macd_histogram=None if pd.isna(row.get("macd_histogram")) else float(row.get("macd_histogram")),
        ema9=None if pd.isna(row.get("ema9")) else float(row.get("ema9")),
        mcginley=None if pd.isna(row.get("mcginley")) else float(row.get("mcginley")),
        sma=None if pd.isna(row.get("sma")) else float(row.get("sma")),
        bias=str(row.get("bias", "neutral")).lower(),
        strength=0.0 if pd.isna(row.get("strength")) else float(row.get("strength")),
    )


def _build_state_series(
    *,
    symbol: str,
    raw_1m: pd.DataFrame,
    timeframe: str,
    market_timezone: str,
    market_open_time: str,
    now_market: datetime,
) -> pd.DataFrame:
    resampled = resample_ohlcv(
        raw_1m,
        timeframe=timeframe,
        timezone_name=market_timezone,
        market_open_time=market_open_time,
        now_market=now_market,
    )
    if resampled.empty:
        return resampled
    return build_indicator_frame(resampled)


def _state_at_or_before(indicator_frame: pd.DataFrame, *, symbol: str, timeframe: str, when: pd.Timestamp) -> SymbolIndicatorState | None:
    if indicator_frame.empty:
        return None
    view = indicator_frame.loc[:when]
    if len(view) < 2:
        return None
    latest_ts = view.index[-1]
    prev_ts = view.index[-2]
    latest = _row_to_point(pd.Timestamp(latest_ts), view.iloc[-1])
    previous = _row_to_point(pd.Timestamp(prev_ts), view.iloc[-2])
    return SymbolIndicatorState(symbol=symbol, timeframe=timeframe, latest=latest, previous=previous)


def _option_chain_at(
    *,
    underlying: str,
    spot_price: float,
    atm_strike: int,
    expiry_date,
    when: pd.Timestamp,
    ce_symbol: str,
    pe_symbol: str,
    ce_frame_5m: pd.DataFrame,
    pe_frame_5m: pd.DataFrame,
) -> OptionChainSnapshot | None:
    if ce_frame_5m.empty or pe_frame_5m.empty:
        return None
    ce_view = ce_frame_5m.loc[:when]
    pe_view = pe_frame_5m.loc[:when]
    if ce_view.empty or pe_view.empty:
        return None

    ce_row = ce_view.iloc[-1]
    pe_row = pe_view.iloc[-1]

    ce_oi = float(ce_row.get("oi", 0.0) or 0.0)
    pe_oi = float(pe_row.get("oi", 0.0) or 0.0)

    ce_prev_oi = float(ce_view.iloc[-2].get("oi", 0.0) or 0.0) if len(ce_view) >= 2 else ce_oi
    pe_prev_oi = float(pe_view.iloc[-2].get("oi", 0.0) or 0.0) if len(pe_view) >= 2 else pe_oi

    contracts = (
        OptionContract(
            security_id="CE",
            strike=float(atm_strike),
            option_type="CE",
            ltp=float(ce_row.get("close", 0.0) or 0.0),
            oi=ce_oi,
            oi_change=ce_oi - ce_prev_oi,
            volume=float(ce_row.get("volume", 0.0) or 0.0),
        ),
        OptionContract(
            security_id="PE",
            strike=float(atm_strike),
            option_type="PE",
            ltp=float(pe_row.get("close", 0.0) or 0.0),
            oi=pe_oi,
            oi_change=pe_oi - pe_prev_oi,
            volume=float(pe_row.get("volume", 0.0) or 0.0),
        ),
    )

    snap_time = when.to_pydatetime() if isinstance(when, pd.Timestamp) else pd.Timestamp(when).to_pydatetime()
    return OptionChainSnapshot(
        underlying=underlying,
        spot_price=float(spot_price),
        atm_strike=int(atm_strike),
        expiry_date=expiry_date,
        snapshot_time=snap_time,
        contracts=contracts,
    )


def run_backtest(
    *,
    days: int,
    timeframe_base: str,
    hold_minutes: int,
    out_csv: Path | None,
    verbose: bool,
    debug_at: str = "",
    log_path: Path | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.market_timezone)
    now_market = datetime.now(tz)
    from_market = now_market - timedelta(days=int(days))

    dhan = DhanClient(settings.dhan)
    if not dhan.configured:
        raise RuntimeError("Dhan API not configured. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")

    index_symbol = settings.nifty_index_symbol.strip().upper()

    scrip_master_path = Path(__file__).resolve().parent / "backend" / "app" / ".cache" / "dhan_api_scrip_master.csv"
    if not scrip_master_path.exists():
        raise RuntimeError(f"Missing Dhan scrip master at {scrip_master_path}. Cannot map historical ATM option security IDs.")

    use_underlying_prefix = "NIFTY"
    strike_step = 50

    if verbose:
        print(f"Loading scrip master: {scrip_master_path}")

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
    scrip_master = scrip_master[
        (scrip_master["SEM_EXM_EXCH_ID"] == "NSE")
        & (scrip_master["SEM_INSTRUMENT_NAME"] == "OPTIDX")
        & (scrip_master["SEM_TRADING_SYMBOL"].astype(str).str.startswith(use_underlying_prefix))
    ].copy()
    if scrip_master.empty:
        raise RuntimeError("No NSE OPTIDX NIFTY rows found in scrip master; cannot map option contracts.")

    scrip_master["SEM_EXPIRY_DATE"] = pd.to_datetime(scrip_master["SEM_EXPIRY_DATE"], errors="coerce")
    scrip_master["SEM_STRIKE_PRICE"] = pd.to_numeric(scrip_master["SEM_STRIKE_PRICE"], errors="coerce")
    scrip_master["SEM_SMST_SECURITY_ID"] = scrip_master["SEM_SMST_SECURITY_ID"].astype(str)
    scrip_master["SEM_OPTION_TYPE"] = scrip_master["SEM_OPTION_TYPE"].astype(str).str.upper()
    scrip_master = scrip_master.dropna(subset=["SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE"])
    # Keep comparisons timezone-safe by using date-only keys (scrip master timestamps are tz-naive).
    scrip_master["EXPIRY_DATE_ONLY"] = scrip_master["SEM_EXPIRY_DATE"].dt.date

    # Fetch 1m data so we can compute spot + ATM selection per candle.
    index_1m = dhan.get_intraday_ohlc(
        security_id=settings.dhan.nifty_security_id,
        exchange_segment=settings.dhan.nifty_exchange_segment,
        instrument=settings.dhan.nifty_instrument,
        from_date=_ensure_tz(from_market, tz).replace(tzinfo=None),
        to_date=_ensure_tz(now_market, tz).replace(tzinfo=None),
        interval_minutes=1,
        include_oi=False,
    )

    # Spot indicators across timeframes (used for index_state).
    index_indicator_by_tf: dict[str, pd.DataFrame] = {
        tf: _build_state_series(
            symbol=index_symbol,
            raw_1m=index_1m,
            timeframe=tf,
            market_timezone=settings.market_timezone,
            market_open_time=settings.market_open_time,
            now_market=now_market,
        )
        for tf in settings.timeframes
    }

    base_frame = index_indicator_by_tf.get(timeframe_base, pd.DataFrame())
    if base_frame.empty:
        raise RuntimeError(f"No data available for base timeframe: {timeframe_base}")

    def atm_strike_for_spot(price: float) -> int:
        if strike_step <= 0:
            return int(round(price))
        return int(round(price / strike_step) * strike_step)

    def pick_expiry(at_time: pd.Timestamp) -> datetime.date:
        # Pick the nearest expiry date >= current date (market time).
        at_date = pd.Timestamp(at_time).date()
        future = scrip_master.loc[scrip_master["EXPIRY_DATE_ONLY"] >= at_date, "EXPIRY_DATE_ONLY"]
        if future.empty:
            return max(scrip_master["EXPIRY_DATE_ONLY"])
        return min(future)

    def resolve_security_id(*, expiry: datetime.date, strike: int, opt_type: str) -> str:
        opt_type = str(opt_type).upper()
        subset = scrip_master[
            (scrip_master["SEM_OPTION_TYPE"] == opt_type)
            & (scrip_master["SEM_STRIKE_PRICE"] == float(strike))
            & (scrip_master["EXPIRY_DATE_ONLY"] == expiry)
            ]
        if subset.empty:
            raise RuntimeError(f"Unable to resolve option security id for {use_underlying_prefix} {expiry} {strike} {opt_type}")
        return str(subset.iloc[0]["SEM_SMST_SECURITY_ID"])

    # Cache option 1m candles per (security_id) so we can build indicators on demand.
    option_1m_cache: dict[str, pd.DataFrame] = {}
    option_indicator_cache: dict[tuple[str, str], pd.DataFrame] = {}
    option_5m_cache: dict[str, pd.DataFrame] = {}

    engine = ScreenerEngine(settings)

    rows_out: list[dict[str, Any]] = []
    trades: list[Trade] = []

    open_trade: Trade | None = None
    hold_delta = timedelta(minutes=int(hold_minutes))
    debug_at_ts: pd.Timestamp | None = pd.Timestamp(debug_at) if debug_at else None

    log_fp: io.TextIOWrapper | None = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "w", encoding="utf-8")  # noqa: SIM115

    def emit(message: str) -> None:
        print(message)
        if log_fp is not None:
            log_fp.write(message + "\n")
            log_fp.flush()

    def get_option_indicator(security_id: str, tf: str) -> pd.DataFrame:
        key = (security_id, tf)
        cached = option_indicator_cache.get(key)
        if cached is not None:
            return cached

        raw = option_1m_cache.get(security_id)
        if raw is None:
            raw = dhan.get_intraday_ohlc(
                security_id=security_id,
                exchange_segment=settings.dhan.options_exchange_segment,
                instrument=settings.dhan.options_instrument,
                from_date=_ensure_tz(from_market, tz).replace(tzinfo=None),
                to_date=_ensure_tz(now_market, tz).replace(tzinfo=None),
                interval_minutes=1,
                include_oi=True,
            )
            option_1m_cache[security_id] = raw

        frame = _build_state_series(
            symbol=security_id,
            raw_1m=raw,
            timeframe=tf,
            market_timezone=settings.market_timezone,
            market_open_time=settings.market_open_time,
            now_market=now_market,
        )
        option_indicator_cache[key] = frame
        return frame

    def get_option_5m(security_id: str) -> pd.DataFrame:
        cached = option_5m_cache.get(security_id)
        if cached is not None:
            return cached
        raw = option_1m_cache.get(security_id)
        if raw is None:
            raw = dhan.get_intraday_ohlc(
                security_id=security_id,
                exchange_segment=settings.dhan.options_exchange_segment,
                instrument=settings.dhan.options_instrument,
                from_date=_ensure_tz(from_market, tz).replace(tzinfo=None),
                to_date=_ensure_tz(now_market, tz).replace(tzinfo=None),
                interval_minutes=1,
                include_oi=True,
            )
            option_1m_cache[security_id] = raw
        frame = resample_ohlcv(
            raw,
            timeframe="5m",
            timezone_name=settings.market_timezone,
            market_open_time=settings.market_open_time,
            now_market=now_market,
        )
        option_5m_cache[security_id] = frame
        return frame

    try:
        for idx in range(1, len(base_frame)):
            when = pd.Timestamp(base_frame.index[idx])

            spot_price = float(base_frame.iloc[idx].get("close", 0.0) or 0.0)
            atm_strike = atm_strike_for_spot(spot_price)
            expiry = pick_expiry(when)
            ce_security_id = resolve_security_id(expiry=expiry, strike=atm_strike, opt_type="CE")
            pe_security_id = resolve_security_id(expiry=expiry, strike=atm_strike, opt_type="PE")

            ce_symbol = f"NIFTY ATM {atm_strike} CE"
            pe_symbol = f"NIFTY ATM {atm_strike} PE"

            states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]] = {}
            missing = False
            for tf in settings.timeframes:
                idx_state = _state_at_or_before(
                    index_indicator_by_tf[tf],
                    symbol=index_symbol,
                    timeframe=tf,
                    when=when,
                )
                ce_ind = get_option_indicator(ce_security_id, tf)
                pe_ind = get_option_indicator(pe_security_id, tf)
                ce_state = _state_at_or_before(ce_ind, symbol=ce_symbol, timeframe=tf, when=when)
                pe_state = _state_at_or_before(pe_ind, symbol=pe_symbol, timeframe=tf, when=when)
                if idx_state is None or ce_state is None or pe_state is None:
                    missing = True
                    break
                states_by_timeframe[tf] = {
                    index_symbol: idx_state,
                    ce_symbol: ce_state,
                    pe_symbol: pe_state,
                }

            if missing:
                continue

            option_chain = _option_chain_at(
                underlying=settings.nifty_index_symbol,
                spot_price=spot_price,
                atm_strike=atm_strike,
                expiry_date=expiry,
                when=when,
                ce_symbol=ce_symbol,
                pe_symbol=pe_symbol,
                ce_frame_5m=get_option_5m(ce_security_id),
                pe_frame_5m=get_option_5m(pe_security_id),
            )

            # Use the same private S2 screener implementation for consistency.
            # previous_option_map isn't used by current S2 logic.
            s2_signals = engine._run_s2(states_by_timeframe, option_chain, {})  # noqa: SLF001
            if not s2_signals:
                continue
            s2 = s2_signals[0]

            if debug_at_ts is not None and when == debug_at_ts:
                payload = s2.payload if isinstance(s2.payload, dict) else {}
                details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
                emit("---- S2 DEBUG ----")
                emit(f"time={when.isoformat()} spot={spot_price:.2f} strike={atm_strike} expiry={expiry}")
                emit(f"ce_security_id={ce_security_id} pe_security_id={pe_security_id}")
                for tf in ("5m", "10m", "15m"):
                    tf_detail = details.get(tf) if isinstance(details.get(tf), dict) else {}
                    idx_d = tf_detail.get("index") if isinstance(tf_detail.get("index"), dict) else {}
                    ce_d = tf_detail.get("atm_ce") if isinstance(tf_detail.get("atm_ce"), dict) else {}
                    pe_d = tf_detail.get("atm_pe") if isinstance(tf_detail.get("atm_pe"), dict) else {}
                    tf_sig = tf_detail.get("timeframe_signal")
                    emit(f"[{tf}] timeframe_signal={tf_sig}")
                    emit(
                        "  index.status={status} close={close} trend={trend} prev_close={prev_close} prev_trend={prev_trend}".format(
                            status=idx_d.get("status"),
                            close=idx_d.get("close"),
                            trend=idx_d.get("trend"),
                            prev_close=idx_d.get("previous_close"),
                            prev_trend=idx_d.get("previous_trend"),
                        )
                    )
                    emit(
                        "  ce.status={status} close={close} trend={trend} prev_close={prev_close} prev_trend={prev_trend}".format(
                            status=ce_d.get("status"),
                            close=ce_d.get("close"),
                            trend=ce_d.get("trend"),
                            prev_close=ce_d.get("previous_close"),
                            prev_trend=ce_d.get("previous_trend"),
                        )
                    )
                    emit(
                        "  pe.status={status} close={close} trend={trend} prev_close={prev_close} prev_trend={prev_trend}".format(
                            status=pe_d.get("status"),
                            close=pe_d.get("close"),
                            trend=pe_d.get("trend"),
                            prev_close=pe_d.get("previous_close"),
                            prev_trend=pe_d.get("previous_trend"),
                        )
                    )
                emit(f"FINAL signal={s2.signal} confidence={s2.confidence} reason={s2.reason}")
                emit("------------------")

            # Simple trading rule:
            # - enter on buy/strong_buy or sell/strong_sell
            # - exit after fixed hold_minutes
            signal = str(s2.signal or "").strip().lower()
            if signal in {"buy", "strong_buy", "sell", "strong_sell"} and open_trade is None:
                open_trade = Trade(
                    entry_time=when.to_pydatetime(),
                    exit_time=when.to_pydatetime(),  # placeholder
                    signal=signal,
                    entry_price=spot_price,
                    exit_price=spot_price,
                )

            if open_trade is not None and when.to_pydatetime() >= (open_trade.entry_time + hold_delta):
                close_trade = Trade(
                    entry_time=open_trade.entry_time,
                    exit_time=when.to_pydatetime(),
                    signal=open_trade.signal,
                    entry_price=open_trade.entry_price,
                    exit_price=spot_price,
                )
                trades.append(close_trade)
                open_trade = None

            rows_out.append(
                {
                    "time": when.isoformat(),
                    "signal": signal,
                    "confidence": float(s2.confidence or 0.0),
                    "spot_price": spot_price,
                }
            )
    finally:
        if log_fp is not None:
            log_fp.close()

    if trades:
        pnl_points = [t.pnl_points for t in trades]
        pnl_pct = [t.pnl_pct for t in trades]
        summary = {
            "trades": len(trades),
            "win_rate_pct": round(100.0 * sum(1 for p in pnl_points if p > 0) / len(trades), 2),
            "avg_pnl_points": round(sum(pnl_points) / len(trades), 4),
            "avg_pnl_pct": round(sum(pnl_pct) / len(trades), 4),
            "total_pnl_points": round(sum(pnl_points), 4),
            "total_pnl_pct": round(sum(pnl_pct), 4),
        }
    else:
        summary = {"trades": 0}

    if out_csv is not None:
        df = pd.DataFrame(rows_out)
        df.to_csv(out_csv, index=False)

    return {
        "params": {
            "days": days,
            "timeframe_base": timeframe_base,
            "hold_minutes": hold_minutes,
            "atm_strike_step": strike_step,
        },
        "summary": summary,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S2 backtest (last N days) using Dhan intraday API data.")
    parser.add_argument("--days", type=int, default=30, help="How many days to fetch (default: 30).")
    parser.add_argument("--base", type=str, default="5m", choices=("5m", "10m", "15m"), help="Signal evaluation timeframe.")
    parser.add_argument("--hold-minutes", type=int, default=30, help="Hold duration for PnL evaluation (default: 30).")
    parser.add_argument("--out", type=str, default="", help="Optional CSV output path for signal stream.")
    parser.add_argument("--debug-at", type=str, default="", help="Print S2 decision details at an exact timestamp (ISO8601).")
    parser.add_argument("--log", type=str, default="backtest.log", help="Write console output to this log file (default: backtest.log).")
    parser.add_argument("--verbose", action="store_true", help="Print extra debug output.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    out_csv = Path(args.out).resolve() if args.out else None
    log_path = Path(args.log).resolve() if args.log else None
    result = run_backtest(
        days=args.days,
        timeframe_base=args.base,
        hold_minutes=args.hold_minutes,
        out_csv=out_csv,
        verbose=bool(args.verbose),
        debug_at=str(args.debug_at or ""),
        log_path=log_path,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
