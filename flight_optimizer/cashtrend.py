"""A trip's cash fare compared with what it has cost before.

Each fresh cash search is stored (`db.record_cash_total`), so today's fare can be set against the fares
seen in the last `LOOKBACK_DAYS`. History is per trip and cabin, and is cleared if the trip's dates change.
"""

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flight_optimizer import db

LOOKBACK_DAYS = 30
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class CashTrend:
    """`current` against the fares seen before this search, within the lookback window."""

    current: int
    previous: tuple[int, ...]  # totals from earlier searches, oldest first
    first_seen: date | None  # when the earliest of them was searched

    @property
    def count(self) -> int:
        return len(self.previous)

    @property
    def low(self) -> int | None:
        return min(self.previous) if self.previous else None

    @property
    def typical(self) -> int | None:
        """The median: a slow drift down moves it slowly, so a real drop still stands out."""
        return round(statistics.median(self.previous)) if self.previous else None


def load_trend(
    conn: sqlite3.Connection, trip_id: int, cabin: str, *, total: int, fetched_at: str, now: datetime | None = None
) -> CashTrend:
    """Record this search and read back how the fare compares with the ones before it."""
    db.record_cash_total(conn, trip_id, cabin, fetched_at, total)
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=LOOKBACK_DAYS)).astimezone(timezone.utc)
    history = db.cash_history(conn, trip_id, cabin, since.strftime(_TIME_FORMAT))
    earlier = [(when, price) for when, price in history if when < fetched_at]
    first = datetime.strptime(earlier[0][0], _TIME_FORMAT).date() if earlier else None
    return CashTrend(current=total, previous=tuple(price for _, price in earlier), first_seen=first)
