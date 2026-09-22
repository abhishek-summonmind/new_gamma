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
class S4Hit:
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


def run_s4_backtest(
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

    # Company-only: exclude index symbol.
    all_symbols = [str(s).strip().upper() for s in settings.nifty_symbols if str(s).strip()]
    if limit_symbols and limit_symbols > 0:
        all_symbols = all_symbols[: int(limit_symbols)]

    scrip_master_path = Path(__file__).resolve().parent / "backend" / "app" / ".cache" / "dhan_api_scrip_master.csv"
    if not scrip_master_path.exists():
        raise RuntimeError(f"Missing Dhan scrip master at {scrip_master_path}. Cannot map equity security IDs.")

    security_map = _load_equity_security_ids(symbols=all_symbols, scrip_master_path=scrip_master_path)
    missing = [s for s in all_symbols if s not in security_map]
    if missing:
        # Keep running for available symbols, but report missing.
        print(f"Warning: missing equity security ids for {len(missing)} symbols (first 10): {missing[:10]}")

    engine = ScreenerEngine(settings)

    # Cache fetched 1m candles and computed indicator frames per symbol/timeframe.
    equity_1m_cache: dict[str, pd.DataFrame] = {}
    indicator_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def get_1m(symbol: str) -> pd.DataFrame:
        cached = equity_1m_cache.get(symbol)
        if cached is not None:
            return cached
        sec_id = security_map.get(symbol)
        if not sec_id:
            equity_1m_cache[symbol] = pd.DataFrame()
            return equity_1m_cache[symbol]
        frame = dhan.get_intraday_ohlc(
            security_id=sec_id,
            exchange_segment=settings.dhan.equity_exchange_segment,
            instrument=settings.dhan.equity_instrument,
            from_date=_ensure_tz(from_market, tz).replace(tzinfo=None),
            to_date=_ensure_tz(now_market, tz).replace(tzinfo=None),
            interval_minutes=1,
            include_oi=False,
        )
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

    hits: list[S4Hit] = []

    # Build evaluation timeline from union of candle times across symbols for each timeframe.
    for timeframe in timeframes:
        # Collect all candle timestamps for which we can evaluate S4.
        all_times: list[pd.Timestamp] = []
        for symbol in all_symbols:
            frame = get_indicator(symbol, timeframe)
            if frame.empty:
                continue
            all_times.append(pd.Timestamp(frame.index[-1]))
        if not all_times:
            continue

        # Evaluate over each symbol's own candle times (more accurate, less work).
        for symbol in all_symbols:
            ind = get_indicator(symbol, timeframe)
            if ind.empty:
                continue

            # Only closed candles: use the indicator frame index as candle-close timestamps.
            for when in ind.index:
                when_ts = pd.Timestamp(when)
                state = _state_at_or_before(ind, symbol=symbol, timeframe=timeframe, when=when_ts)
                if state is None:
                    continue

                # Reuse backend screener implementation for consistency.
                out = engine._run_s4({symbol: state})  # noqa: SLF001
                if not out:
                    continue
                row = out[0]
                payload = row.payload if isinstance(row.payload, dict) else {}
                hits.append(
                    S4Hit(
                        time=state.latest.candle_time,
                        symbol=symbol,
                        timeframe=timeframe,
                        signal=str(row.signal or ""),
                        confidence=float(row.confidence or 0.0),
                        reason=str(row.reason or ""),
                        payload=payload,
                    )
                )

    hits.sort(key=lambda h: (h.time, h.confidence), reverse=False)

    if out_csv is not None:
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
                    "price_structure": h.payload.get("price_structure"),
                    "price_move_pct": h.payload.get("price_move_pct"),
                    "price_threshold_pct": h.payload.get("price_threshold_pct"),
                    "volume_vs_ma": h.payload.get("volume_vs_ma"),
                    "volume_confirmed": h.payload.get("volume_confirmed"),
                    "cascade_n": h.payload.get("cascade_n"),
                    "cascade_d": h.payload.get("cascade_d"),
                }
            )
        pd.DataFrame(rows).to_csv(out_csv, index=False)

    summary = {
        "hits": len(hits),
        "real_buy": sum(1 for h in hits if h.signal == "real_buy"),
        "real_sell": sum(1 for h in hits if h.signal == "real_sell"),
        "fake_buy": sum(1 for h in hits if h.signal == "fake_buy"),
        "fake_sell": sum(1 for h in hits if h.signal == "fake_sell"),
        "watch_fake_buy": sum(1 for h in hits if h.signal == "watch_fake_buy"),
        "watch_fake_sell": sum(1 for h in hits if h.signal == "watch_fake_sell"),
        "fake_buy_90": sum(1 for h in hits if h.signal == "fake_buy_90"),
        "fake_sell_90": sum(1 for h in hits if h.signal == "fake_sell_90"),
    }

    return {
        "params": {"days": days, "timeframes": timeframes, "symbols": len(all_symbols)},
        "summary": summary,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S4 backtest (companies only) using Dhan intraday API data.")
    parser.add_argument("--days", type=int, default=10, help="How many days to fetch (default: 10).")
    parser.add_argument(
        "--timeframes",
        type=str,
        default="5m",
        help="Comma-separated timeframes to evaluate (default: 5m). Example: 5m,10m,15m",
    )
    parser.add_argument("--out", type=str, default="s4_signals.csv", help="CSV output path (default: s4_signals.csv).")
    parser.add_argument("--limit-symbols", type=int, default=0, help="Limit number of symbols (0 = all).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    tfs = tuple(tf.strip().lower() for tf in str(args.timeframes).split(",") if tf.strip())
    if not tfs:
        raise SystemExit("No timeframes provided.")

    out_csv = Path(args.out).resolve() if args.out else None
    result = run_s4_backtest(days=args.days, timeframes=tfs, out_csv=out_csv, limit_symbols=int(args.limit_symbols or 0))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
