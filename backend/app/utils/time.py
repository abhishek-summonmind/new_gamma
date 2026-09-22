from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ist_now_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def to_ist_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)
