from __future__ import annotations

import logging
import math
from datetime import datetime, time, timezone
from typing import Any

from ..config import Settings, normalize_market_symbol
from .cache_service import CacheService
from .option_types import OptionChainSnapshot, OptionContract

logger = logging.getLogger(__name__)


def canonical_option_symbol(underlying: str, strike: int | float | str, option_type: str) -> str:
    underlying_norm = normalize_market_symbol(underlying)
    strike_value = float(strike)
    strike_text = str(int(strike_value)) if strike_value.is_integer() else f"{strike_value:g}"
    option_type_norm = str(option_type or "").strip().upper()
    if option_type_norm not in {"CE", "PE"}:
        raise ValueError(f"Unsupported option type: {option_type}")
    return f"{underlying_norm} {strike_text} {option_type_norm}"


def option_metric_cache_key(
    metric: str,
    underlying: str,
    strike: int | float | str,
    option_type: str,
    timeframe: str = "5m",
) -> str:
    metric_norm = str(metric or "").strip().lower()
    timeframe_norm = str(timeframe or "5m").strip().lower()
    return (
        f"option:{metric_norm}:"
        f"{canonical_option_symbol(underlying, strike, option_type)}:"
        f"{timeframe_norm}"
    )


def option_metric_cache_key_from_symbol(metric: str, option_symbol: str, timeframe: str = "5m") -> str:
    parts = str(option_symbol or "").replace("_", " ").replace("-", " ").upper().split()
    option_type = parts[-1] if parts and parts[-1] in {"CE", "PE"} else ""
    body = [part for part in parts[:-1] if part != "ATM"]
    strike_index = next(
        (idx for idx in range(len(body) - 1, -1, -1) if _is_float(body[idx])),
        None,
    )
    if strike_index is None or not option_type:
        raise ValueError(f"Cannot canonicalize option symbol: {option_symbol}")
    return option_metric_cache_key(
        metric,
        " ".join(body[:strike_index]),
        body[strike_index],
        option_type,
        timeframe,
    )


class OptionDeltaCacheProducer:
    """Populate the exact delta keys consumed by S9 before screening."""

    def __init__(self, settings: Settings, cache: CacheService | None = None):
        self._settings = settings
        self._cache = cache or CacheService(settings)

    def populate(self, option_chain: OptionChainSnapshot | None, *, timeframe: str = "5m") -> dict[str, int]:
        summary = {"contracts": 0, "provider": 0, "calculated": 0, "missing": 0}
        if option_chain is None:
            logger.warning("S9 delta cache population skipped: option chain unavailable")
            return summary

        for contract in option_chain.contracts:
            summary["contracts"] += 1
            delta = _valid_delta(getattr(contract, "delta", None))
            source = "provider_response"
            if delta is None:
                delta = self._calculate_delta(option_chain, contract)
                source = "black_scholes_estimate"
            if delta is None:
                summary["missing"] += 1
                continue

            key = option_metric_cache_key(
                "delta",
                option_chain.underlying,
                contract.strike,
                contract.option_type,
                timeframe,
            )
            self._cache.set_json(
                key,
                {
                    "delta": delta,
                    "source": source,
                    "underlying": str(option_chain.underlying).strip().upper(),
                    "strike": float(contract.strike),
                    "option_type": str(contract.option_type).strip().upper(),
                    "timeframe": str(timeframe).strip().lower(),
                    "expiry": option_chain.expiry_date.isoformat(),
                    "as_of": option_chain.snapshot_time.isoformat(),
                },
            )
            summary["provider" if source == "provider_response" else "calculated"] += 1

        logger.info(
            "S9 delta cache populated underlying=%s expiry=%s timeframe=%s "
            "contracts=%s provider=%s calculated=%s missing=%s",
            option_chain.underlying,
            option_chain.expiry_date.isoformat(),
            timeframe,
            summary["contracts"],
            summary["provider"],
            summary["calculated"],
            summary["missing"],
        )
        return summary

    @staticmethod
    def _calculate_delta(option_chain: OptionChainSnapshot, contract: OptionContract) -> float | None:
        spot = _positive_float(option_chain.spot_price)
        strike = _positive_float(contract.strike)
        premium = _positive_float(contract.ltp)
        option_type = str(contract.option_type or "").strip().upper()
        if spot is None or strike is None or premium is None or option_type not in {"CE", "PE"}:
            return None

        expiry_at = datetime.combine(option_chain.expiry_date, time(15, 30), tzinfo=timezone.utc)
        snapshot = option_chain.snapshot_time
        if snapshot.tzinfo is None:
            snapshot = snapshot.replace(tzinfo=timezone.utc)
        else:
            snapshot = snapshot.astimezone(timezone.utc)
        years = max((expiry_at - snapshot).total_seconds() / (365.0 * 86400.0), 1.0 / (365.0 * 24.0))
        rate = 0.065
        volatility = _implied_volatility(
            option_type=option_type,
            spot=spot,
            strike=strike,
            years=years,
            rate=rate,
            premium=premium,
        )
        if volatility is None:
            volatility = 0.35
        d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / (
            volatility * math.sqrt(years)
        )
        call_delta = _normal_cdf(d1)
        delta = call_delta if option_type == "CE" else call_delta - 1.0
        return round(delta, 6)


def _implied_volatility(
    *,
    option_type: str,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    premium: float,
) -> float | None:
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if premium + 1e-9 < intrinsic:
        return None
    low, high = 0.01, 5.0
    for _ in range(80):
        mid = (low + high) / 2.0
        price = _black_scholes_price(option_type, spot, strike, years, rate, mid)
        if price > premium:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def _black_scholes_price(
    option_type: str,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
) -> float:
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    discount = math.exp(-rate * years)
    if option_type == "CE":
        return spot * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)
    return strike * discount * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _is_float(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _valid_delta(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and -1.0 <= parsed <= 1.0 else None
