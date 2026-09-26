from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

try:
    import pandas_ta as ta  # type: ignore
except ModuleNotFoundError:
    ta = None


@dataclass(frozen=True)
class IndicatorPoint:
    candle_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_ma: float | None
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    ema9: float | None = None
    ema20: float | None = None
    ema200: float | None = None
    mcginley: float | None = None
    sma: float | None = None
    bias: str = "neutral"
    strength: float = 0.0
    vwap: float | None = None
    volume_ma20: float | None = None
    stoch_rsi_k: float | None = None
    stoch_rsi_d: float | None = None
    stoch_rsi_k_5: float | None = None
    stoch_rsi_d_5: float | None = None
    supertrend: float | None = None
    supertrend_direction: str | None = None
    supertrend_factor_1_5: float | None = None
    supertrend_direction_factor_1_5: str | None = None


def _latest(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        return None
    return float(value)


def rsi_series(close_series: pd.Series, length: int = 14) -> pd.Series:
    if ta is not None:
        result = ta.rsi(close_series, length=length)
        if result is not None:
            return result

    delta = close_series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = losses.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return rsi


def macd_series(close_series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    if ta is not None:
        frame = ta.macd(close_series, fast=fast, slow=slow, signal=signal)
        if frame is not None and not frame.empty:
            macd_col = next((column for column in frame.columns if column.startswith("MACD_")), None)
            signal_col = next((column for column in frame.columns if column.startswith("MACDs_")), None)
            hist_col = next((column for column in frame.columns if column.startswith("MACDh_")), None)

            if macd_col and signal_col and hist_col:
                return frame[macd_col], frame[signal_col], frame[hist_col]

    fast_ema = close_series.ewm(span=fast, adjust=False).mean()
    slow_ema = close_series.ewm(span=slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_histogram = macd - macd_signal
    return macd, macd_signal, macd_histogram


def mcginley_series(close_series: pd.Series, length: int = 14) -> pd.Series:
    if close_series.empty:
        return close_series

    mg = pd.Series(index=close_series.index, dtype="float64")
    mg.iloc[0] = float(close_series.iloc[0])

    for idx in range(1, len(close_series)):
        price = float(close_series.iloc[idx])
        previous = float(mg.iloc[idx - 1])
        safe_previous = previous if abs(previous) > 1e-9 else 1e-9
        ratio = max(price / safe_previous, 1e-9)
        denominator = max(length * (ratio**4), 1e-9)
        mg.iloc[idx] = previous + (price - previous) / denominator

    return mg


def sma_series(close_series: pd.Series, length: int = 14) -> pd.Series:
    return close_series.rolling(window=length, min_periods=length).mean()


def stoch_rsi_series(
    close_series: pd.Series,
    *,
    rsi_length: int = 14,
    stochastic_length: int = 14,
    k_length: int = 3,
    d_length: int = 3,
) -> tuple[pd.Series, pd.Series]:
    rsi = rsi_series(close_series, length=rsi_length)
    lowest = rsi.rolling(window=stochastic_length, min_periods=stochastic_length).min()
    highest = rsi.rolling(window=stochastic_length, min_periods=stochastic_length).max()
    denominator = highest - lowest
    stoch = ((rsi - lowest) / denominator.replace(0, pd.NA)) * 100.0
    stoch = stoch.where(denominator != 0, 50.0)
    k = stoch.rolling(window=k_length, min_periods=k_length).mean()
    d = k.rolling(window=d_length, min_periods=d_length).mean()
    return k, d


def vwap_series(frame: pd.DataFrame) -> pd.Series:
    typical = (frame["high"].astype(float) + frame["low"].astype(float) + frame["close"].astype(float)) / 3.0
    volume = frame["volume"].astype(float)
    grouped_dates = pd.Series(frame.index.date, index=frame.index)
    cumulative_pv = (typical * volume).groupby(grouped_dates).cumsum()
    cumulative_volume = volume.groupby(grouped_dates).cumsum()
    return cumulative_pv / cumulative_volume.replace(0, pd.NA)


def supertrend_series(
    frame: pd.DataFrame,
    *,
    length: int = 10,
    factor: float = 1.0,
) -> tuple[pd.Series, pd.Series]:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + (factor * atr)
    basic_lower = hl2 - (factor * atr)

    final_upper = pd.Series(index=frame.index, dtype="float64")
    final_lower = pd.Series(index=frame.index, dtype="float64")
    trend = pd.Series(index=frame.index, dtype="object")
    line = pd.Series(index=frame.index, dtype="float64")

    for idx in range(len(frame)):
        if pd.isna(atr.iloc[idx]):
            continue
        if idx == 0 or pd.isna(final_upper.iloc[idx - 1]) or pd.isna(final_lower.iloc[idx - 1]):
            final_upper.iloc[idx] = basic_upper.iloc[idx]
            final_lower.iloc[idx] = basic_lower.iloc[idx]
            trend.iloc[idx] = "up" if close.iloc[idx] >= hl2.iloc[idx] else "down"
        else:
            final_upper.iloc[idx] = (
                basic_upper.iloc[idx]
                if basic_upper.iloc[idx] < final_upper.iloc[idx - 1] or close.iloc[idx - 1] > final_upper.iloc[idx - 1]
                else final_upper.iloc[idx - 1]
            )
            final_lower.iloc[idx] = (
                basic_lower.iloc[idx]
                if basic_lower.iloc[idx] > final_lower.iloc[idx - 1] or close.iloc[idx - 1] < final_lower.iloc[idx - 1]
                else final_lower.iloc[idx - 1]
            )
            if str(trend.iloc[idx - 1]) == "down":
                trend.iloc[idx] = "up" if close.iloc[idx] > final_upper.iloc[idx] else "down"
            else:
                trend.iloc[idx] = "down" if close.iloc[idx] < final_lower.iloc[idx] else "up"
        line.iloc[idx] = final_lower.iloc[idx] if trend.iloc[idx] == "up" else final_upper.iloc[idx]

    return line, trend


def classify_bias(*, close: float, rsi: float | None, macd_histogram: float | None, mcginley: float | None) -> str:
    if None in (rsi, macd_histogram, mcginley):
        return "neutral"

    if close > mcginley and rsi >= 55 and macd_histogram > 0:
        return "bullish"
    if close < mcginley and rsi <= 45 and macd_histogram < 0:
        return "bearish"
    return "neutral"


def compute_strength(*, rsi: float | None, macd_histogram: float | None, close: float, mcginley: float | None) -> float:
    rsi_component = 0.0 if rsi is None else min(abs(rsi - 50) / 50.0, 1.0)
    macd_component = 0.0 if macd_histogram is None else min(abs(macd_histogram) / max(abs(close), 1.0) * 100.0, 1.0)
    trend_component = 0.0 if mcginley is None else min(abs(close - mcginley) / max(abs(close), 1.0) * 50.0, 1.0)
    return round((0.45 * rsi_component) + (0.35 * macd_component) + (0.20 * trend_component), 4)


def build_indicator_frame(frame: pd.DataFrame, *, rsi_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame

    result = frame.copy()
    close_series = result["close"].astype(float)
    volume_series = result["volume"].astype(float)

    rsi_source = rsi_frame if rsi_frame is not None else result
    rsi = rsi_series(rsi_source["close"].astype(float), length=14)
    if rsi_frame is not None:
        # Keep the primary frame and every non-RSI indicator unchanged. An
        # unfinished current bar uses the most recent completed RSI value.
        rsi = rsi.reindex(result.index).ffill()
    macd, macd_signal, macd_histogram = macd_series(close_series, fast=12, slow=26, signal=9)
    mcginley = mcginley_series(close_series, length=14)
    ema9 = close_series.ewm(span=9, adjust=False).mean()
    ema20 = close_series.ewm(span=20, adjust=False).mean()
    ema200 = close_series.ewm(span=200, adjust=False, min_periods=200).mean()
    sma = sma_series(close_series, length=14)
    vol_ma = sma_series(volume_series, length=14)
    vol_ma20 = sma_series(volume_series, length=20)
    vwap = vwap_series(result)
    stoch_k, stoch_d = stoch_rsi_series(close_series, k_length=3, d_length=3)
    stoch_k_5, stoch_d_5 = stoch_rsi_series(close_series, k_length=5, d_length=5)
    supertrend, supertrend_direction = supertrend_series(result, factor=1.0)
    supertrend_1_5, supertrend_direction_1_5 = supertrend_series(result, factor=1.5)

    result["rsi"] = rsi
    result["macd"] = macd
    result["macd_signal"] = macd_signal
    result["macd_histogram"] = macd_histogram
    result["ema9"] = ema9
    result["ema20"] = ema20
    result["ema200"] = ema200
    result["mcginley"] = mcginley
    result["sma"] = sma
    result["volume_ma"] = vol_ma
    result["volume_ma20"] = vol_ma20
    result["vwap"] = vwap
    result["stoch_rsi_k"] = stoch_k
    result["stoch_rsi_d"] = stoch_d
    result["stoch_rsi_k_5"] = stoch_k_5
    result["stoch_rsi_d_5"] = stoch_d_5
    result["supertrend"] = supertrend
    result["supertrend_direction"] = supertrend_direction
    result["supertrend_factor_1_5"] = supertrend_1_5
    result["supertrend_direction_factor_1_5"] = supertrend_direction_1_5

    result["bias"] = [
        classify_bias(
            close=float(close),
            rsi=_safe_float(rsi_value),
            macd_histogram=_safe_float(hist_value),
            mcginley=_safe_float(mg_value),
        )
        for close, rsi_value, hist_value, mg_value in zip(
            result["close"],
            result["rsi"],
            result["macd_histogram"],
            result["mcginley"],
            strict=False,
        )
    ]

    result["strength"] = [
        compute_strength(
            rsi=_safe_float(rsi_value),
            macd_histogram=_safe_float(hist_value),
            close=float(close),
            mcginley=_safe_float(mg_value),
        )
        for close, rsi_value, hist_value, mg_value in zip(
            result["close"],
            result["rsi"],
            result["macd_histogram"],
            result["mcginley"],
            strict=False,
        )
    ]

    return result


def latest_indicator_point(frame: pd.DataFrame) -> IndicatorPoint | None:
    if frame.empty:
        return None

    row = frame.iloc[-1]
    timestamp = frame.index[-1]

    if isinstance(timestamp, pd.Timestamp):
        candle_time = timestamp.to_pydatetime()
    else:
        candle_time = pd.Timestamp(timestamp).to_pydatetime()

    return IndicatorPoint(
        candle_time=candle_time,
        open=float(row.get("open", 0.0)),
        high=float(row.get("high", 0.0)),
        low=float(row.get("low", 0.0)),
        close=float(row.get("close", 0.0)),
        volume=float(row.get("volume", 0.0)),
        volume_ma=_safe_float(row.get("volume_ma")),
        rsi=_safe_float(row.get("rsi")),
        macd=_safe_float(row.get("macd")),
        macd_signal=_safe_float(row.get("macd_signal")),
        macd_histogram=_safe_float(row.get("macd_histogram")),
        ema9=_safe_float(row.get("ema9")),
        ema20=_safe_float(row.get("ema20")),
        ema200=_safe_float(row.get("ema200")),
        mcginley=_safe_float(row.get("mcginley")),
        sma=_safe_float(row.get("sma")),
        bias=str(row.get("bias", "neutral")).lower(),
        strength=float(row.get("strength", 0.0)),
        vwap=_safe_float(row.get("vwap")),
        volume_ma20=_safe_float(row.get("volume_ma20")),
        stoch_rsi_k=_safe_float(row.get("stoch_rsi_k")),
        stoch_rsi_d=_safe_float(row.get("stoch_rsi_d")),
        stoch_rsi_k_5=_safe_float(row.get("stoch_rsi_k_5")),
        stoch_rsi_d_5=_safe_float(row.get("stoch_rsi_d_5")),
        supertrend=_safe_float(row.get("supertrend")),
        supertrend_direction=(
            None
            if row.get("supertrend_direction") is None or pd.isna(row.get("supertrend_direction"))
            else str(row.get("supertrend_direction")).lower()
        ),
        supertrend_factor_1_5=_safe_float(row.get("supertrend_factor_1_5")),
        supertrend_direction_factor_1_5=(
            None
            if row.get("supertrend_direction_factor_1_5") is None or pd.isna(row.get("supertrend_direction_factor_1_5"))
            else str(row.get("supertrend_direction_factor_1_5")).lower()
        ),
    )


def previous_indicator_point(frame: pd.DataFrame) -> IndicatorPoint | None:
    if len(frame) < 2:
        return None
    previous_frame = frame.iloc[:-1]
    return latest_indicator_point(previous_frame)


__all__ = [
    "IndicatorPoint",
    "build_indicator_frame",
    "latest_indicator_point",
    "previous_indicator_point",
    "rsi_series",
    "macd_series",
    "mcginley_series",
    "sma_series",
    "stoch_rsi_series",
    "supertrend_series",
    "vwap_series",
    "classify_bias",
    "compute_strength",
]
