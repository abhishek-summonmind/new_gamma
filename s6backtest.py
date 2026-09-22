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
from backend.app.utils.indicators import IndicatorPoint, build_indicator_frame, macd_series
from backend.app.utils.timeframe import resample_ohlcv, take_lookback


@dataclass(frozen=True)
class S6Hit:
    time: datetime
    symbol: str
    timeframe: str
    action: str
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


def _pivot_flags(series: pd.Series, *, lookback: int) -> tuple[bool, bool]:
    """
    Return (is_hh, is_ll) for the last element compared against the prior lookback values.
    Strict inequality: equality does not count.
    """
    if series is None or series.empty or len(series) < lookback + 1:
        return False, False
    window = series.iloc[-(lookback + 1) : -1].astype(float)
    latest = float(series.iloc[-1])
    return latest > float(window.max()), latest < float(window.min())


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


def run_s6_backtest(
    *,
    days: int,
    timeframes: tuple[str, ...],
    out_csv: Path | None,
    limit_symbols: int = 0,
    pivot_lookback: int = 10,
) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.market_timezone)
    now_market = datetime.now(tz)
    from_market = now_market - timedelta(days=int(days))

    dhan = DhanClient(settings.dhan)
    if not dhan.configured:
        raise RuntimeError("Dhan API not configured. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")

    # S6 is company-only (NIFTY symbols), index excluded by snapshot service.
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

    # Backtest "state" (cache replacement): S6 streak + (action, confidence) + signal_time per symbol/timeframe.
    prev_state: dict[tuple[str, str], dict[str, Any]] = {}

    def finalize_state(
        *,
        symbol: str,
        timeframe: str,
        raw_divergence: str | None,
        candle_time: datetime,
    ) -> tuple[str, str | None, int, str | None]:
        key = (timeframe, symbol)
        state = prev_state.get(key) or {}

        prev_action = str(state.get("action") or "").strip().lower()
        prev_confidence = state.get("confidence")
        prev_signal_time = state.get("signal_time")
        prev_streak_type = str(state.get("streak_type") or "").strip().lower() or None
        prev_streak_count = int(state.get("streak_count") or 0)

        streak_type: str | None = None
        streak_count = 0
        if raw_divergence in {"buy", "sell"}:
            if prev_streak_type == raw_divergence:
                streak_type = raw_divergence
                streak_count = prev_streak_count + 1
            else:
                streak_type = raw_divergence
                streak_count = 1

        action = "watch"
        confidence: str | None = None
        if streak_count <= 0:
            action = "watch"
            confidence = None
        elif streak_count == 1:
            action = "watch"
            confidence = "observed"
        elif streak_count == 2:
            action = "fake_buy" if streak_type == "buy" else "fake_sell"
            confidence = "confirmed"
        else:
            action = "fake_buy" if streak_type == "buy" else "fake_sell"
            confidence = "high_90"

        same_state = (prev_action == action) and (prev_confidence == confidence)
        signal_time = prev_signal_time if same_state and prev_signal_time else candle_time.isoformat()

        prev_state[key] = {
            "action": action,
            "confidence": confidence,
            "signal_time": signal_time,
            "streak_type": streak_type,
            "streak_count": int(streak_count),
        }

        return signal_time, confidence, int(streak_count), streak_type

    hits: list[S6Hit] = []
    price_threshold = 0.005  # must match service

    for timeframe in timeframes:
        tf = str(timeframe).strip().lower()
        if tf not in {"5m", "10m", "15m"}:
            continue

        needed = int(pivot_lookback) + 2

        for symbol in symbols:
            raw_1m = get_1m(symbol)
            ind = get_indicator(symbol, tf)
            if raw_1m.empty or ind.empty:
                continue

            for when in ind.index:
                # Use the candle close timestamp as evaluation point.
                when_ts = pd.Timestamp(when)
                view = ind.loc[:when_ts]
                if len(view) < 2:
                    continue

                latest_ts = view.index[-1]
                prev_ts = view.index[-2]
                latest_point = _row_to_point(pd.Timestamp(latest_ts), view.iloc[-1])
                prev_point = _row_to_point(pd.Timestamp(prev_ts), view.iloc[-2])

                close = float(latest_point.close)
                prev_close = float(prev_point.close)
                macd = latest_point.macd
                prev_macd = prev_point.macd
                if macd is None or prev_macd is None:
                    continue
                if abs(prev_close) < 1e-9:
                    continue

                price_delta_pct = (close - prev_close) / prev_close
                raw_divergence: str | None = None
                price_hh = None
                price_ll = None
                macd_hh = None
                macd_ll = None

                if abs(price_delta_pct) >= price_threshold:
                    # Build resampled frame for pivot detection at this point.
                    try:
                        resampled = resample_ohlcv(
                            raw_1m.loc[:when_ts],
                            timeframe=tf,
                            timezone_name=settings.market_timezone,
                            market_open_time=settings.market_open_time,
                            now_market=now_market,
                        )
                    except Exception:  # noqa: BLE001
                        resampled = pd.DataFrame()

                    lookback = take_lookback(resampled, candles=max(needed, 60)) if not resampled.empty else None
                    if lookback is not None and not lookback.empty and len(lookback) >= needed:
                        closes = lookback["close"].astype(float)
                        macd_line, _sig, _hist = macd_series(closes, fast=12, slow=26, signal=9)
                        macd_line = macd_line.astype(float)

                        high = lookback["high"].astype(float)
                        low = lookback["low"].astype(float)

                        price_hh, _ = _pivot_flags(high, lookback=int(pivot_lookback))
                        _, price_ll = _pivot_flags(low, lookback=int(pivot_lookback))
                        macd_hh, macd_ll = _pivot_flags(macd_line.ffill().fillna(0.0), lookback=int(pivot_lookback))

                        price_up = close > prev_close
                        price_down = close < prev_close
                        macd_up = float(macd) > float(prev_macd)
                        macd_down = float(macd) < float(prev_macd)

                        if price_up and macd_down and bool(price_hh) and (not bool(macd_hh)):
                            raw_divergence = "buy"
                        elif price_down and macd_up and bool(price_ll) and (not bool(macd_ll)):
                            raw_divergence = "sell"

                signal_time, conf_label, streak_count, streak_type = finalize_state(
                    symbol=symbol,
                    timeframe=tf,
                    raw_divergence=raw_divergence,
                    candle_time=latest_point.candle_time,
                )
                action = str(prev_state.get((tf, symbol), {}).get("action") or "watch")

                if action not in {"fake_buy", "fake_sell"}:
                    continue

                confidence = 0.62
                if price_hh is True or price_ll is True:
                    confidence += 0.06
                if macd_hh is False or macd_ll is False:
                    confidence += 0.05
                if conf_label == "high_90":
                    confidence = max(confidence, 0.9)
                confidence = max(0.0, min(1.0, float(confidence)))

                hits.append(
                    S6Hit(
                        time=latest_point.candle_time,
                        symbol=symbol,
                        timeframe=tf,
                        action=action,
                        confidence=confidence,
                        reason="Price–MACD divergence with pivot confirmation.",
                        payload={
                            "timeframe": tf,
                            "signal_time": signal_time,
                            "confidence": conf_label,
                            "streak_count": streak_count,
                            "streak_type": streak_type,
                            "close": round(close, 2),
                            "prev_close": round(prev_close, 2),
                            "macd": round(float(macd), 4),
                            "prev_macd": round(float(prev_macd), 4),
                            "delta_price": round(close - prev_close, 2),
                            "delta_macd": round(float(macd) - float(prev_macd), 4),
                            "price_hh": price_hh,
                            "price_ll": price_ll,
                            "macd_hh": macd_hh,
                            "macd_ll": macd_ll,
                        },
                    )
                )

    hits.sort(key=lambda h: (h.time, h.confidence), reverse=False)

    if out_csv is not None:
        columns = [
            "time",
            "symbol",
            "timeframe",
            "action",
            "confidence",
            "reason",
            "signal_time",
            "confidence_label",
            "streak_count",
            "streak_type",
            "close",
            "prev_close",
            "macd",
            "prev_macd",
            "delta_price",
            "delta_macd",
            "price_hh",
            "price_ll",
            "macd_hh",
            "macd_ll",
        ]
        rows: list[dict[str, Any]] = []
        for h in hits:
            p = h.payload
            rows.append(
                {
                    "time": h.time.isoformat(),
                    "symbol": h.symbol,
                    "timeframe": h.timeframe,
                    "action": h.action,
                    "confidence": round(float(h.confidence), 4),
                    "reason": h.reason,
                    "signal_time": p.get("signal_time"),
                    "confidence_label": p.get("confidence"),
                    "streak_count": p.get("streak_count"),
                    "streak_type": p.get("streak_type"),
                    "close": p.get("close"),
                    "prev_close": p.get("prev_close"),
                    "macd": p.get("macd"),
                    "prev_macd": p.get("prev_macd"),
                    "delta_price": p.get("delta_price"),
                    "delta_macd": p.get("delta_macd"),
                    "price_hh": p.get("price_hh"),
                    "price_ll": p.get("price_ll"),
                    "macd_hh": p.get("macd_hh"),
                    "macd_ll": p.get("macd_ll"),
                }
            )
        pd.DataFrame(rows, columns=columns).to_csv(out_csv, index=False)

    summary = {
        "hits": len(hits),
        "fake_buy": sum(1 for h in hits if h.action == "fake_buy"),
        "fake_sell": sum(1 for h in hits if h.action == "fake_sell"),
        "high_90": sum(1 for h in hits if str(h.payload.get("confidence") or "") == "high_90"),
    }

    return {"params": {"days": days, "timeframes": timeframes, "symbols": len(symbols), "pivot_lookback": pivot_lookback}, "summary": summary}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6 backtest (companies only) using Dhan intraday API data.")
    parser.add_argument("--days", type=int, default=10, help="How many days to fetch (default: 10).")
    parser.add_argument(
        "--timeframes",
        type=str,
        default="5m",
        help="Comma-separated timeframes to evaluate (default: 5m). Example: 5m,10m,15m",
    )
    parser.add_argument("--out", type=str, default="s6_signals.csv", help="CSV output path (default: s6_signals.csv).")
    parser.add_argument("--limit-symbols", type=int, default=0, help="Limit number of symbols (0 = all).")
    parser.add_argument("--pivot-lookback", type=int, default=10, help="Pivot lookback candles (default: 10).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    tfs = tuple(tf.strip().lower() for tf in str(args.timeframes).split(",") if tf.strip())
    if not tfs:
        raise SystemExit("No timeframes provided.")

    out_csv = Path(args.out).resolve() if args.out else None
    result = run_s6_backtest(
        days=int(args.days),
        timeframes=tfs,
        out_csv=out_csv,
        limit_symbols=int(args.limit_symbols or 0),
        pivot_lookback=int(args.pivot_lookback or 10),
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
