from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import IndicatorSnapshot
from ..utils.time import to_ist_naive
from ..utils.indicators import IndicatorPoint, build_indicator_frame, latest_indicator_point, previous_indicator_point
from ..utils.timeframe import resample_ohlcv, take_lookback

logger = logging.getLogger(__name__)


@dataclass
class SymbolIndicatorState:
    symbol: str
    timeframe: str
    latest: IndicatorPoint
    previous: IndicatorPoint | None


@dataclass
class IndicatorComputationResult:
    primary_timeframe: str
    states_by_symbol: dict[str, SymbolIndicatorState] = field(default_factory=dict)
    states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]] = field(default_factory=dict)


class IndicatorEngine:
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def compute_and_store(
        self,
        *,
        db: Session,
        run_id: int,
        frames_by_symbol: dict[str, pd.DataFrame],
        now_market: datetime | None = None,
    ) -> IndicatorComputationResult:
        primary_timeframe = self._settings.timeframes[0]
        result = IndicatorComputationResult(primary_timeframe=primary_timeframe)

        target_symbol = (self._settings.underlying_symbol or self._settings.nifty_index_symbol).strip().upper()
        for timeframe in self._settings.timeframes:
            bucket: dict[str, SymbolIndicatorState] = {}
            completed_trend_bucket: dict[str, SymbolIndicatorState] = {}

            for symbol, raw_frame in frames_by_symbol.items():
                if str(symbol).strip().upper() != target_symbol:
                    continue
                if raw_frame.empty:
                    continue

                resampled = resample_ohlcv(
                    raw_frame,
                    timeframe=timeframe,
                    timezone_name=self._settings.market_timezone,
                    market_open_time=self._settings.market_open_time,
                    now_market=now_market,
                )
                lookback = take_lookback(resampled, candles=self._settings.lookback_candles)
                if lookback.empty:
                    continue

                rsi_frame = None
                if timeframe == "15m":
                    completed_cutoff = None
                    if now_market is not None:
                        market_time = now_market
                        if market_time.tzinfo is None:
                            market_time = market_time.replace(tzinfo=ZoneInfo(self._settings.market_timezone))
                        else:
                            market_time = market_time.astimezone(ZoneInfo(self._settings.market_timezone))
                        completed_cutoff = pd.Timestamp(market_time - timedelta(microseconds=1))
                    completed_15m = resampled
                    if completed_cutoff is not None:
                        completed_15m = completed_15m.loc[completed_15m.index <= completed_cutoff]
                    rsi_frame = take_lookback(completed_15m, candles=50)
                    logger.info(
                        "RSI_INPUT timeframe=15m completed_rows=%s latest=%s",
                        len(rsi_frame),
                        rsi_frame.index[-1].isoformat() if not rsi_frame.empty else None,
                    )

                indicator_frame = build_indicator_frame(lookback, rsi_frame=rsi_frame)
                latest = latest_indicator_point(indicator_frame)
                previous = previous_indicator_point(indicator_frame)
                if latest is None:
                    continue

                state = SymbolIndicatorState(
                    symbol=symbol,
                    timeframe=timeframe,
                    latest=latest,
                    previous=previous,
                )
                bucket[symbol] = state

                if timeframe in {"15m", "30m", "60m"}:
                    completed_state = self._completed_s9_trend_state(
                        symbol=symbol,
                        timeframe=timeframe,
                        raw_frame=raw_frame,
                        now_market=now_market,
                    )
                    if completed_state is not None:
                        completed_trend_bucket[symbol] = completed_state

                # logger.info(
                #     "INDICATOR %s %s | RSI=%s MACD=%s MACD_SIGNAL=%s McGinley=%s Close=%s",
                #     symbol,
                #     timeframe,
                #     self._fmt_float(latest.rsi),
                #     self._fmt_float(latest.macd),
                #     self._fmt_float(latest.macd_signal),
                #     self._fmt_float(latest.mcginley),
                #     self._fmt_float(latest.close),
                # )

                self._upsert_indicator_snapshot(
                    db=db,
                    run_id=run_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    latest=latest,
                )

            result.states_by_timeframe[timeframe] = bucket
            if timeframe in {"15m", "30m", "60m"}:
                result.states_by_timeframe[f"s9_{timeframe}_completed"] = completed_trend_bucket
            if timeframe == primary_timeframe:
                result.states_by_symbol = bucket

        return result

    def _completed_s9_trend_state(
        self,
        *,
        symbol: str,
        timeframe: str,
        raw_frame: pd.DataFrame,
        now_market: datetime | None,
    ) -> SymbolIndicatorState | None:
        completed = resample_ohlcv(
            raw_frame,
            timeframe=timeframe,
            timezone_name=self._settings.market_timezone,
            market_open_time=self._settings.market_open_time,
            market_close_time=self._settings.market_close_time,
            now_market=now_market,
            completed_only=True,
        )
        lookback = take_lookback(completed, candles=self._settings.lookback_candles)
        if lookback.empty:
            return None
        indicator_frame = build_indicator_frame(lookback)
        latest = latest_indicator_point(indicator_frame)
        if latest is None:
            return None
        return SymbolIndicatorState(
            symbol=symbol,
            timeframe=timeframe,
            latest=latest,
            previous=previous_indicator_point(indicator_frame),
        )

    def _upsert_indicator_snapshot(
        self,
        *,
        db: Session,
        run_id: int,
        symbol: str,
        timeframe: str,
        latest: IndicatorPoint,
    ) -> None:
        candle_time = self._to_db_time(latest.candle_time)
        db.execute(
            delete(IndicatorSnapshot).where(
                IndicatorSnapshot.symbol == symbol,
                IndicatorSnapshot.timeframe == timeframe,
                IndicatorSnapshot.candle_time == candle_time,
            )
        )

        db.add(
            IndicatorSnapshot(
                run_id=run_id,
                symbol=symbol,
                timeframe=timeframe,
                candle_time=candle_time,
                close_price=float(latest.close),
                volume=float(latest.volume),
                rsi=latest.rsi,
                macd=latest.macd,
                macd_signal=latest.macd_signal,
                macd_histogram=latest.macd_histogram,
                mcginley=latest.mcginley,
                bias=latest.bias,
                strength=float(latest.strength),
            )
        )

    @staticmethod
    def _fmt_float(value: float | None) -> str:
        if value is None:
            return "None"
        return f"{float(value):.4f}"

    @staticmethod
    def _to_db_time(value: datetime) -> datetime:
        return to_ist_naive(value)
