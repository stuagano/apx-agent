from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .ledger import LedgerEntry


class FreshnessError(Exception):
    """Raised on an unparseable freshness/duration value."""


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp, coercing naive values to UTC so comparisons
    against an aware `now` never raise."""
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(s: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s)
    if not m:
        raise FreshnessError(f"bad duration {s!r}; expected forms like '24h', '30m', '2d'")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def waiver_active(entry: LedgerEntry | None, now: datetime) -> bool:
    if entry is None or not entry.waiver:
        return False
    return now < parse_iso(entry.waiver["until"])
