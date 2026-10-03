"""Trip dates: what a trip's dates mean, and how far either side of them the award search reaches.

By default a trip's dates are the days to LEAVE: the outbound leaves on the outbound date and the
return leaves on the return date (owner's rule, 2026-10-03). The owner can choose, per trip and only
explicitly, to anchor the outbound on the day to ARRIVE instead (`date_anchor = 'arrive'`): the
outbound must land on that day and may leave up to `EARLY_DEPARTURE_DAYS` before it (an evening or
overnight flight the day before counts; landing on an earlier day does not). The return date is always
the day to leave.

A trip is either exact (the default) or flexible, which widens the award search by `FLEXIBLE_WINDOW_DAYS`
either side of each date (owner's rule, 2026-10-03): with a departure anchor the days the flights leave,
with an arrival anchor the days the outbound lands (and the return leaves). Cash fares always search
departures on the trip's exact dates.
"""

import sqlite3
from datetime import date, timedelta

FLEXIBLE_WINDOW_DAYS = 3  # owner's rule: 3 days either side
EARLY_DEPARTURE_DAYS = 1  # owner's rule: the outbound may leave this many days before the day it lands
MIN_STAY_NIGHTS = 1  # owner's rule (2026-10-03): the return leaves at least this many days after the outbound lands


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
    """(days the outbound may leave, days it may land) for a trip due to land on `arrive_on`, give or take
    `days`. It may leave up to `EARLY_DEPARTURE_DAYS` before the first landing day.

    None if every allowed landing day has passed. Neither window starts before today.
    """
    landing = date_window(arrive_on, days, today)
    if landing is None:
        return None
    first, last = landing
    leaving = (max(first - timedelta(days=EARLY_DEPARTURE_DAYS), today or date.today()), last)
    return leaving, landing


def is_arrival(trip: sqlite3.Row) -> bool:
    """True when the trip's outbound date is the day to land rather than the day to leave."""
    return trip["date_anchor"] == "arrive"


def window_note(trip: sqlite3.Row) -> str:
    """Text to put after a trip's dates: nothing for an exact trip."""
    return f" (flexible: +/-{FLEXIBLE_WINDOW_DAYS} days)" if trip["flexible"] else ""


def flexibility_label(trip: sqlite3.Row) -> str:
    """A short label for a list column: "exact" or "+/-3 days"."""
    return f"+/-{FLEXIBLE_WINDOW_DAYS} days" if trip["flexible"] else "exact"


def trip_dates(trip: sqlite3.Row, fmt: str = "%b %d %Y") -> str:
    """A trip's dates saying what they mean, e.g. "leave Jan 09 2027, return Jan 16 2027"."""
    outbound = format(date.fromisoformat(trip["outbound_date"]), fmt)
    text = f"arrive {outbound}" if is_arrival(trip) else f"leave {outbound}"
    if trip["return_date"]:
        text += f", return {date.fromisoformat(trip['return_date']):{fmt}}"
    return text


def option_dates(option, trip: sqlite3.Row, fmt: str = "%b %d") -> str:
    """An option's dates in the trip's terms: "Jan 09 - Jan 16" (the days the outbound and return leave).
    For an arrival-anchored trip the first date is the day it lands, with the outbound's own departure day
    when it leaves early, e.g. "Jan 09 (leave Jan 08) - Jan 16"."""
    if is_arrival(trip):
        text = format(option.arrive_date, fmt)
        if option.depart_date != option.arrive_date:
            text += f" (leave {option.depart_date:{fmt}})"
    else:
        text = format(option.depart_date, fmt)
    if option.return_date is not None:
        text += f" - {option.return_date:{fmt}}"
    return text


def asked_dates(trip: sqlite3.Row, fmt: str = "%b %d") -> str:
    text = format(date.fromisoformat(trip["outbound_date"]), fmt)
    if trip["return_date"]:
        text += f" - {date.fromisoformat(trip['return_date']):{fmt}}"
    return text


def on_asked_dates(trip: sqlite3.Row, option) -> bool:
    """True when the option is on the days asked: leaving on them, or for an arrival-anchored trip landing on
    the outbound date (leaving a day early still counts) and leaving on the return date."""
    asked_return = date.fromisoformat(trip["return_date"]) if trip["return_date"] else None
    outbound = option.arrive_date if is_arrival(trip) else option.depart_date
    return outbound == date.fromisoformat(trip["outbound_date"]) and option.return_date == asked_return
