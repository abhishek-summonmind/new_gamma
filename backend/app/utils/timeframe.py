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
    market_close_time: str = "15:30",
    now_market: datetime | None = None,
    completed_only: bool = False,
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
            closed="left" if completed_only else "right",
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

    if completed_only and not aggregated.empty:
        close_hours, close_minutes = _parse_hhmm(market_close_time)
        session_minutes = ((close_hours * 60) + close_minutes) - ((open_hours * 60) + open_minutes)
        candle_end_minutes = (
            (aggregated.index.hour * 60)
            + aggregated.index.minute
            - ((open_hours * 60) + open_minutes)
        )
        full_session_candle = (
            (candle_end_minutes > 0)
            & (candle_end_minutes <= session_minutes)
            & ((candle_end_minutes % minutes) == 0)
        )
        aggregated = aggregated.loc[full_session_candle]

    completion_cutoff = now_market
    if completed_only and completion_cutoff is None and not normalized.empty:
        completion_cutoff = normalized.index[-1].to_pydatetime()

    if completion_cutoff is not None and not aggregated.empty:
        market_time = completion_cutoff
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
