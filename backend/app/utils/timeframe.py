from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


def timeframe_to_minutes(timeframe: str) -> int:
    if not timeframe.endswith("m"):
        raise ValueError(f"Unsupported timeframe format: {timeframe}")
    return int(timeframe[:-1])


def normalize_ohlcv_frame(frame: pd.DataFrame, timezone_name: str) -> pd.DataFrame:
    if frame.empty:
        return frame

    normalized = frame.copy()

    if normalized.index.tz is None:
        normalized.index = normalized.index.tz_localize("UTC")

    normalized.index = normalized.index.tz_convert(ZoneInfo(timezone_name))
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    normalized = normalized.sort_index()
    return normalized


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM value: {value}")
    hours, minutes = parts
    return int(hours), int(minutes)


def resample_ohlcv(
    frame: pd.DataFrame,
    timeframe: str,
    timezone_name: str,
    *,
    market_open_time: str = "09:15",
    now_market: datetime | None = None,
) -> pd.DataFrame:
    minutes = timeframe_to_minutes(timeframe)
    if frame.empty:
        return frame

    normalized = normalize_ohlcv_frame(frame, timezone_name)

    open_hours, open_minutes = _parse_hhmm(market_open_time)
    offset = pd.Timedelta(hours=open_hours, minutes=open_minutes)

    aggregated = (
        normalized.resample(
            f"{minutes}min",
            origin="start_day",
            offset=offset,
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
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
    )

    if now_market is not None and not aggregated.empty:
        market_time = now_market
        if isinstance(market_time, pd.Timestamp):
            market_time = market_time.to_pydatetime()

        if market_time.tzinfo is None:
            market_time = market_time.replace(tzinfo=ZoneInfo(timezone_name))
        else:
            market_time = market_time.astimezone(ZoneInfo(timezone_name))

        aggregated = aggregated.loc[aggregated.index <= market_time]

    return aggregated


def take_lookback(frame: pd.DataFrame, candles: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame.tail(candles)
