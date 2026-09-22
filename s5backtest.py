from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from backend.app.config import get_settings
from backend.app.services.dhan_client import DhanClient
from backend.app.services.indicator_engine import SymbolIndicatorState
from backend.app.services.screener_engine import ScreenerEngine
from backend.app.utils.indicators import IndicatorPoint, build_indicator_frame
from backend.app.utils.timeframe import resample_ohlcv


@dataclass(frozen=True)
class S5Hit:
    time: datetime
    symbol: str
    timeframe: str
    signal: str
    confidence: float
    reason: str
    payload: dict[str, Any]


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
        mcginley=None if pd.isna(row.get("mcginley")) else float(row.get("mcginley")),
        sma=None if pd.isna(row.get("sma")) else float(row.get("sma")),
        bias=str(row.get("bias", "neutral")).lower(),
        strength=0.0 if pd.isna(row.get("strength")) else float(row.get("strength")),
    )


def _build_indicator_frame(
    *,
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


def _state_at_or_before(
    indicator_frame: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    when: pd.Timestamp,
) -> SymbolIndicatorState | None:
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


def _load_equity_security_ids(*, symbols: Iterable[str], scrip_master_path: Path) -> dict[str, str]:
    needed = {str(sym).strip().upper() for sym in symbols if str(sym).strip()}
    if not needed:
        return {}

    usecols = [
        "SEM_EXM_EXCH_ID",
        "SEM_INSTRUMENT_NAME",
        "SEM_TRADING_SYMBOL",
        "SEM_SERIES",
        "SEM_SMST_SECURITY_ID",
    ]

    found: dict[str, str] = {}
    for chunk in pd.read_csv(scrip_master_path, usecols=usecols, chunksize=200_000, low_memory=False):
        chunk["SEM_EXM_EXCH_ID"] = chunk["SEM_EXM_EXCH_ID"].astype(str).str.upper()
        chunk["SEM_INSTRUMENT_NAME"] = chunk["SEM_INSTRUMENT_NAME"].astype(str).str.upper()
        chunk["SEM_TRADING_SYMBOL"] = chunk["SEM_TRADING_SYMBOL"].astype(str).str.upper()
        chunk["SEM_SERIES"] = chunk["SEM_SERIES"].astype(str).str.upper()
        chunk["SEM_SMST_SECURITY_ID"] = chunk["SEM_SMST_SECURITY_ID"].astype(str)

        chunk = chunk[
            (chunk["SEM_EXM_EXCH_ID"] == "NSE")
            & (chunk["SEM_INSTRUMENT_NAME"] == "EQUITY")
            & (chunk["SEM_SERIES"] == "EQ")
            & (chunk["SEM_TRADING_SYMBOL"].isin(needed))
        ]
        if chunk.empty:
            continue

        for _, row in chunk.iterrows():
            sym = str(row["SEM_TRADING_SYMBOL"]).strip().upper()
            sec = str(row["SEM_SMST_SECURITY_ID"]).strip()
            if sym and sec and sym in needed and sym not in found:
                found[sym] = sec

        if len(found) >= len(needed):
            break

    return found


def run_s5_backtest(
    *,
    days: int,
    timeframes: tuple[str, ...],
    out_csv: Path | None,
    limit_symbols: int = 0,
) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.market_timezone)
    now_market = datetime.now(tz)
    from_market = now_market - timedelta(days=int(days))

    dhan = DhanClient(settings.dhan)
    if not dhan.configured:
        raise RuntimeError("Dhan API not configured. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")

    # S5 is company-only by design (backend excludes index). We'll backtest only companies.
    symbols = [str(s).strip().upper() for s in settings.nifty_symbols if str(s).strip()]
    if limit_symbols and limit_symbols > 0:
        symbols = symbols[: int(limit_symbols)]

    scrip_master_path = Path(__file__).resolve().parent / "backend" / "app" / ".cache" / "dhan_api_scrip_master.csv"
    if not scrip_master_path.exists():
        raise RuntimeError(f"Missing Dhan scrip master at {scrip_master_path}. Cannot map equity security IDs.")

    security_map = _load_equity_security_ids(symbols=symbols, scrip_master_path=scrip_master_path)
    missing = [s for s in symbols if s not in security_map]
    if missing:
        print(f"Warning: missing equity security ids for {len(missing)} symbols (first 10): {missing[:10]}")

    engine = ScreenerEngine(settings)

    equity_1m_cache: dict[str, pd.DataFrame] = {}
    indicator_cache: dict[tuple[str, str], pd.DataFrame] = {}

    pivot_lookback = 10
    price_threshold = 0.005  # 0.5% of price (keep spec-like threshold)

    def fetch_1m_range(*, sec_id: str) -> pd.DataFrame:
        """
        Fetch intraday 1m candles for a potentially large range by chunking requests.

        Keeps S5 logic unchanged; this only makes `--days 90` practical with APIs that
        limit how far back intraday data can be fetched in a single call.
        """
        chunk_days = int(getattr(settings, "intraday_fetch_days", 14) or 14)
        chunk_days = max(1, min(chunk_days, 30))

        start = _ensure_tz(from_market, tz).replace(tzinfo=None)
        end = _ensure_tz(now_market, tz).replace(tzinfo=None)
        if start >= end:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        cursor = start
        while cursor < end:
            next_end = min(end, cursor + timedelta(days=chunk_days))
            try:
                part = dhan.get_intraday_ohlc(
                    security_id=sec_id,
                    exchange_segment=settings.dhan.equity_exchange_segment,
                    instrument=settings.dhan.equity_instrument,
                    from_date=cursor,
                    to_date=next_end,
                    interval_minutes=1,
                    include_oi=False,
                )
            except Exception:  # noqa: BLE001
                part = pd.DataFrame()
            if part is not None and not part.empty:
                frames.append(part)
            cursor = next_end

        if not frames:
            return pd.DataFrame()

        merged = pd.concat(frames, axis=0, ignore_index=False)
        merged = merged[~merged.index.duplicated(keep="last")]
        merged = merged.sort_index()
        return merged

    def get_1m(symbol: str) -> pd.DataFrame:
        cached = equity_1m_cache.get(symbol)
        if cached is not None:
            return cached
        sec_id = security_map.get(symbol)
        if not sec_id:
            equity_1m_cache[symbol] = pd.DataFrame()
            return equity_1m_cache[symbol]
        frame = fetch_1m_range(sec_id=str(sec_id))
        equity_1m_cache[symbol] = frame
        return frame

    def get_indicator(symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        cached = indicator_cache.get(key)
        if cached is not None:
            return cached
        raw = get_1m(symbol)
        frame = (
            _build_indicator_frame(
                raw_1m=raw,
                timeframe=timeframe,
                market_timezone=settings.market_timezone,
                market_open_time=settings.market_open_time,
                now_market=now_market,
            )
            if not raw.empty
            else pd.DataFrame()
        )
        indicator_cache[key] = frame
        return frame

    hits: list[S5Hit] = []

    for timeframe in timeframes:
        tf = str(timeframe).strip().lower()
        if tf not in {"5m", "10m", "15m"}:
            continue

        for symbol in symbols:
            ind = get_indicator(symbol, tf)
            if ind.empty:
                continue

            # Pivot-based divergence:
            # - Fake Buy: price makes HH but RSI does not confirm HH
            # - Fake Sell: price makes LL but RSI does not confirm LL
            required_cols = {"high", "low", "close", "rsi"}
            if not required_cols.issubset(set(ind.columns)):
                continue

            work = ind.dropna(subset=["high", "low", "close", "rsi"]).copy()
            if len(work) < pivot_lookback + 2:
                continue

            high = work["high"].astype(float)
            low = work["low"].astype(float)
            close = work["close"].astype(float)
            rsi = work["rsi"].astype(float)

            prev_high_max = high.shift(1).rolling(pivot_lookback, min_periods=pivot_lookback).max()
            prev_low_min = low.shift(1).rolling(pivot_lookback, min_periods=pivot_lookback).min()
            prev_rsi_max = rsi.shift(1).rolling(pivot_lookback, min_periods=pivot_lookback).max()
            prev_rsi_min = rsi.shift(1).rolling(pivot_lookback, min_periods=pivot_lookback).min()

            price_hh = high > prev_high_max
            price_ll = low < prev_low_min
            rsi_hh = rsi > prev_rsi_max
            rsi_ll = rsi < prev_rsi_min

            prev_close = close.shift(1)
            price_delta_pct = (close - prev_close) / prev_close.replace(0.0, pd.NA)

            # Iterate only where we have enough history for pivots.
            for when_ts in work.index:
                if pd.isna(prev_high_max.loc[when_ts]) or pd.isna(prev_low_min.loc[when_ts]):
                    continue

                dp = price_delta_pct.loc[when_ts]
                if dp is None or pd.isna(dp):
                    continue

                dp = float(dp)
                if abs(dp) < price_threshold:
                    continue

                action = None
                if dp > 0 and bool(price_hh.loc[when_ts]) and (not bool(rsi_hh.loc[when_ts])):
                    action = "fake_buy"
                    reason = "Divergence: price made HH but RSI did not confirm."
                elif dp < 0 and bool(price_ll.loc[when_ts]) and (not bool(rsi_ll.loc[when_ts])):
                    action = "fake_sell"
                    reason = "Divergence: price made LL but RSI did not confirm."
                else:
                    continue

                # Build a state for timestamp + confidence (keep same bounded confidence style).
                state = _state_at_or_before(work, symbol=symbol, timeframe=tf, when=pd.Timestamp(when_ts))
                if state is None:
                    continue

                rsi_delta = abs(float(rsi.loc[when_ts]) - float(rsi.shift(1).loc[when_ts]))
                confidence = engine._bounded_confidence(  # noqa: SLF001
                    0.56 + min(abs(dp) * 6.0, 0.28) + min(rsi_delta / 12.0, 0.18)
                )

                hits.append(
                    S5Hit(
                        time=state.latest.candle_time,
                        symbol=symbol,
                        timeframe=tf,
                        signal=action,
                        confidence=float(confidence),
                        reason=reason,
                        payload={
                            "timeframe": tf,
                            "signal_time": state.latest.candle_time.isoformat(),
                            "close": round(float(close.loc[when_ts]), 2),
                            "prev_close": round(float(prev_close.loc[when_ts]), 2),
                            "rsi": round(float(rsi.loc[when_ts]), 2),
                            "prev_rsi": round(float(rsi.shift(1).loc[when_ts]), 2),
                            "delta_price": round(float(close.loc[when_ts] - prev_close.loc[when_ts]), 2),
                            "delta_rsi": round(float(rsi.loc[when_ts] - rsi.shift(1).loc[when_ts]), 2),
                            "price_move_pct": round(dp * 100.0, 3),
                            "price_threshold_pct": round(price_threshold * 100.0, 3),
                            "price_hh": bool(price_hh.loc[when_ts]),
                            "price_ll": bool(price_ll.loc[when_ts]),
                            "rsi_hh": bool(rsi_hh.loc[when_ts]),
                            "rsi_ll": bool(rsi_ll.loc[when_ts]),
                            "pivot_lookback": int(pivot_lookback),
                        },
                    )
                )

    hits.sort(key=lambda h: (h.time, h.confidence), reverse=False)

    if out_csv is not None:
        columns = [
            "time",
            "symbol",
            "timeframe",
            "signal",
            "confidence",
            "reason",
            "price_move_pct",
            "rsi_move",
            "divergence",
            "volume_vs_ma",
        ]
        rows = []
        for h in hits:
            rows.append(
                {
                    "time": h.time.isoformat(),
                    "symbol": h.symbol,
                    "timeframe": h.timeframe,
                    "signal": h.signal,
                    "confidence": round(float(h.confidence), 4),
                    "reason": h.reason,
                    "price_move_pct": h.payload.get("price_move_pct"),
                    "rsi_move": h.payload.get("rsi_move"),
                    "divergence": h.payload.get("divergence"),
                    "volume_vs_ma": h.payload.get("volume_vs_ma"),
                }
            )
        pd.DataFrame(rows, columns=columns).to_csv(out_csv, index=False)

    summary = {
        "hits": len(hits),
        "fake_buy": sum(1 for h in hits if h.signal == "fake_buy"),
        "fake_sell": sum(1 for h in hits if h.signal == "fake_sell"),
    }

    return {
        "params": {"days": days, "timeframes": timeframes, "symbols": len(symbols)},
        "summary": summary,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S5 backtest (companies only) using Dhan intraday API data.")
    parser.add_argument("--days", type=int, default=10, help="How many days to fetch (default: 10).")
    parser.add_argument(
        "--timeframes",
        type=str,
        default="5m",
        help="Comma-separated timeframes to evaluate (default: 5m). Example: 5m,10m,15m",
    )
    parser.add_argument("--out", type=str, default="s5_signals.csv", help="CSV output path (default: s5_signals.csv).")
    parser.add_argument("--limit-symbols", type=int, default=0, help="Limit number of symbols (0 = all).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    tfs = tuple(tf.strip().lower() for tf in str(args.timeframes).split(",") if tf.strip())
    if not tfs:
        raise SystemExit("No timeframes provided.")

    out_csv = Path(args.out).resolve() if args.out else None
    result = run_s5_backtest(
        days=args.days,
        timeframes=tfs,
        out_csv=out_csv,
        limit_symbols=int(args.limit_symbols or 0),
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
