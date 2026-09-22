"""Flexible travel windows: how far either side of a trip's dates the award search reaches.

A trip's outbound date is the day the owner wants to LAND, and its return date the day they want
to LEAVE (owner's rule, 2026-09-22). The outbound may leave up to `EARLY_DEPARTURE_DAYS` before it
lands, so an overnight or late flight the day before counts; landing on an earlier day does not.

A trip is either exact (the default) or flexible. Flexible means the owner can land up to
`FLEXIBLE_WINDOW_DAYS` EARLIER, never later (the arrival date is a deadline, owner's rule 2026-09-22),
and leave for home up to that many days earlier or later than the return date.
"""

import sqlite3
from datetime import date, timedelta

FLEXIBLE_WINDOW_DAYS = 3  # owner's rule: 3 days either side
EARLY_DEPARTURE_DAYS = 1  # owner's rule: the outbound may leave this many days before the day it lands


def window_days(trip: sqlite3.Row) -> int:
    """0 for an exact trip, otherwise the days either side to search."""
    return FLEXIBLE_WINDOW_DAYS if trip["flexible"] else 0


def date_window(on: date, days: int, today: date | None = None) -> tuple[date, date] | None:
    """The first and last date to search around `on`, never in the past. None if the whole window has passed."""
    start, end = on - timedelta(days=days), on + timedelta(days=days)
    start = max(start, today or date.today())
    return None if end < start else (start, end)


def outbound_windows(
    arrive_on: date, days: int, today: date | None = None
) -> tuple[tuple[date, date], tuple[date, date]] | None:
    """(days the outbound may leave, days it may land) for a trip due to land on `arrive_on`.

    None if every allowed landing day has passed. Neither window starts before today.
    """
    first = max(arrive_on - timedelta(days=days), today or date.today())
    last = arrive_on  # the arrival date is the latest day to land
    if last < first:
        return None
    landing = (first, last)
    leaving = (max(first - timedelta(days=EARLY_DEPARTURE_DAYS), today or date.today()), last)
    return leaving, landing


def window_note(trip: sqlite3.Row) -> str:
    """Text to put after a trip's dates: nothing for an exact trip."""
    return f" (flexible: arrive up to {FLEXIBLE_WINDOW_DAYS} days early, return +/-{FLEXIBLE_WINDOW_DAYS})" if trip["flexible"] else ""


def option_dates(option, fmt: str = "%b %d") -> str:
    """An option's dates by the trip's meaning: the day it lands, then the day the return leaves,
    with the outbound's own departure day when it leaves early, e.g. "Jan 09 (leave Jan 08) - Jan 16"."""
    text = format(option.arrive_date, fmt)
    if option.depart_date != option.arrive_date:
        text += f" (leave {option.depart_date:{fmt}})"
    if option.return_date is not None:
        text += f" - {option.return_date:{fmt}}"
    return text


def asked_dates(trip: sqlite3.Row, fmt: str = "%b %d") -> str:
    text = format(date.fromisoformat(trip["depart_date"]), fmt)
    if trip["return_date"]:
        text += f" - {date.fromisoformat(trip['return_date']):{fmt}}"
    return text


def on_asked_dates(trip: sqlite3.Row, option) -> bool:
    """True when the option lands and returns on the days asked (leaving a day early still counts)."""
    asked_return = date.fromisoformat(trip["return_date"]) if trip["return_date"] else None
    return option.arrive_date == date.fromisoformat(trip["depart_date"]) and option.return_date == asked_return
