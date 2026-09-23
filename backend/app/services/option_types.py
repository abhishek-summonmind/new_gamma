from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class OptionContract:
    security_id: str
    strike: float
    option_type: str
    ltp: float
    oi: float
    oi_change: float
    volume: float
    delta: float | None = None
    iv: float | None = None
    theta: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    vega: float | None = None
    gamma: float | None = None


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying: str
    spot_price: float
    atm_strike: int
    expiry_date: date
    snapshot_time: datetime
    contracts: tuple[OptionContract, ...]
    requested_expiry: date | None = None
    fallback_used: bool = False



