"""Flexible travel windows: how far either side of a trip's dates the award search reaches.

A trip is either exact (the default) or flexible. Flexible means the owner can travel up to
`FLEXIBLE_WINDOW_DAYS` earlier or later, on both the outbound and the return date.
"""

import sqlite3
from datetime import date, timedelta

FLEXIBLE_WINDOW_DAYS = 3  # owner's rule: 3 days either side


def window_days(trip: sqlite3.Row) -> int:
    """0 for an exact trip, otherwise the days either side to search."""
    return FLEXIBLE_WINDOW_DAYS if trip["flexible"] else 0


def date_window(on: date, days: int, today: date | None = None) -> tuple[date, date] | None:
    """The first and last date to search around `on`, never in the past. None if the whole window has passed."""
    start, end = on - timedelta(days=days), on + timedelta(days=days)
    start = max(start, today or date.today())
    return None if end < start else (start, end)


def window_note(trip: sqlite3.Row) -> str:
    """Text to put after a trip's dates: nothing for an exact trip."""
    return f" (flexible: +/-{FLEXIBLE_WINDOW_DAYS} days)" if trip["flexible"] else ""
