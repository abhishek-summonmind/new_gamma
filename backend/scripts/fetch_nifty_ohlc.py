from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.services.dhan_client import DhanClient  # noqa: E402


def _timeframe_minutes(timeframe: str) -> int:
    value = str(timeframe).strip().lower()
    if not value.endswith("m"):
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return int(value[:-1])


def _fmt_ts(value, timezone_name: str) -> str:
    ts = value
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")


def fetch_once(*, timeframes: list[str], tail: int, lookback_days: int) -> None:
    settings = get_settings()
    client = DhanClient(settings.dhan)
    if not client.configured:
        raise RuntimeError("Dhan credentials missing. Check DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env")

    now_market = datetime.now(ZoneInfo(settings.market_timezone))
    fetch_from = now_market - timedelta(days=max(1, int(lookback_days)))

    print("=" * 90)
    print(f"NIFTY OHLC fetch @ {now_market.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(
        "security_id="
        f"{settings.dhan.nifty_security_id} segment={settings.dhan.nifty_exchange_segment} "
        f"instrument={settings.dhan.nifty_instrument}"
    )

    for timeframe in timeframes:
        minutes = _timeframe_minutes(timeframe)
        frame = client.get_intraday_ohlc(
            security_id=settings.dhan.nifty_security_id,
            exchange_segment=settings.dhan.nifty_exchange_segment,
            instrument=settings.dhan.nifty_instrument,
            from_date=fetch_from,
            to_date=now_market,
            interval_minutes=minutes,
            include_oi=False,
        )
        if frame.empty:
            print(f"\n{timeframe}: no candles returned")
            continue

        market_open_hour, market_open_minute = [int(part) for part in str(settings.market_open_time).split(":")[:2]]
        session_start = now_market.replace(
            hour=market_open_hour,
            minute=market_open_minute,
            second=0,
            microsecond=0,
        )
        elapsed_minutes = max(0, int((now_market - session_start).total_seconds() // 60))
        current_candle_start = session_start + timedelta(minutes=(elapsed_minutes // minutes) * minutes)
        latest_completed_candle_start = current_candle_start - timedelta(minutes=minutes)
        if latest_completed_candle_start < session_start:
            latest_completed_candle_start = session_start
        cutoff_utc = latest_completed_candle_start.astimezone(ZoneInfo("UTC"))
        frame = frame.loc[frame.index <= cutoff_utc]
        if frame.empty:
            print(f"\n{timeframe}: no completed candles returned")
            continue

        print(f"\n{timeframe} latest {min(tail, len(frame))} completed candles:")
        print("time                 open       high       low        close      move")
        print("-" * 78)
        for ts, row in frame.tail(tail).iterrows():
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])
            move = round(close_price - open_price, 2)
            print(
                f"{_fmt_ts(ts, settings.market_timezone)}  "
                f"{open_price:9.2f} {high_price:9.2f} {low_price:9.2f} "
                f"{close_price:10.2f} {move:9.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch live NIFTY OHLC candles from Dhan API.")
    parser.add_argument("--timeframes", default="5m,10m,15m", help="Comma-separated timeframes, e.g. 5m,10m,15m")
    parser.add_argument("--tail", type=int, default=3, help="Number of latest candles to print per timeframe")
    parser.add_argument("--lookback-days", type=int, default=1, help="Lookback days for intraday fetch")
    parser.add_argument("--watch", type=int, default=0, help="Repeat every N seconds. 0 means run once")
    args = parser.parse_args()

    timeframes = [row.strip() for row in args.timeframes.split(",") if row.strip()]
    if not timeframes:
        raise ValueError("At least one timeframe is required")

    while True:
        fetch_once(timeframes=timeframes, tail=max(1, args.tail), lookback_days=max(1, args.lookback_days))
        if args.watch <= 0:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
