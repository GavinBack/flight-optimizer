"""Tell the owner when the daily check itself is not working.

The daily run is silent by design when there is nothing to say, so a broken run looked exactly like a
quiet day. This module turns two kinds of trouble into an ntfy message:

- Problems during a run: a trip that could not be searched (quota, network, a bug), cash prices or
  Claude advice that could not be fetched.
- A missed run: no check was even attempted for over `MISSED_AFTER` (the Mac was off or asleep, or the
  schedule was stopped). The next run says so.

These are about the tool, not about a trip, so they do not follow the `active` rule for trip alerts; they
only happen in a run that had active trips. They never include balances, and an unexpected error is
described by its type only, because its text could echo request details.

The same problem is not repeated within `FAILURE_REPEAT`, so a broken key nags once a day and not on
every manual retry.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flight_optimizer import db
from flight_optimizer.advisor import AdvisorError
from flight_optimizer.cash_fares import SerpApiError
from flight_optimizer.seats_aero import SeatsAeroError

# The job runs at 7:00 daily, so 20 hours lets tomorrow's run repeat a still-broken problem
# while a retry the same morning stays quiet.
FAILURE_REPEAT = timedelta(hours=20)
MISSED_AFTER = timedelta(hours=36)
LOG_HINT = "Log: logs/notify.log"

_LAST_ATTEMPT = "notify:last_attempt"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Problem:
    where: str  # what was being done, e.g. "Costa Rica" or "Claude advice"
    what: str  # safe text: never balances or request details
    severe: bool = True  # False for an extra that failed (cash prices, advice): the run itself worked


@dataclass(frozen=True)
class FailureAlert:
    key: str
    title: str
    message: str
    priority: int
    tags: tuple[str, ...]
    click: str | None = None


def describe(exc: BaseException) -> str:
    """Text safe to send: the tool's own errors are worded not to echo requests; anything else is
    named by its type only."""
    if isinstance(exc, (SeatsAeroError, SerpApiError, AdvisorError)):
        return str(exc)[:200]
    return f"unexpected {type(exc).__name__}"


def _utc(now: datetime) -> datetime:
    # A naive `now` is read as local time; astimezone() makes any input UTC.
    return now.astimezone(timezone.utc)


def build_failure_alert(problems: list[Problem]) -> FailureAlert:
    severe = any(p.severe for p in problems)
    lines = [f"- {p.where}: {p.what}" for p in problems]
    if severe:
        title = "Flight check failed" if len(problems) == 1 else f"Flight check: {len(problems)} problems"
        lines.append("Anything the check could still do was done, but this needs a look.")
    else:
        title = "Flight check: something is degraded"
        lines.append("The check itself worked; only the extras above are missing.")
    lines.append(LOG_HINT)
    key = hashlib.sha256("|".join(sorted(f"{p.where}:{p.what}" for p in problems)).encode()).hexdigest()[:16]
    return FailureAlert(
        key=key, title=title, message="\n".join(lines), priority=4 if severe else 2,
        tags=("rotating_light",) if severe else ("warning",),
    )


def decide_failure_alert(
    conn: sqlite3.Connection, problems: list[Problem], *, now: datetime
) -> FailureAlert | None:
    """An alert for these problems, or None if there are none or the same ones were reported recently."""
    if not problems:
        return None
    alert = build_failure_alert(problems)
    last = db.get_meta(conn, f"failure:{alert.key}")
    if last is not None and _utc(now) - datetime.fromisoformat(last) < FAILURE_REPEAT:
        return None
    return alert


def record_failure_sent(conn: sqlite3.Connection, alert: FailureAlert, *, now: datetime) -> None:
    db.set_meta(conn, f"failure:{alert.key}", _utc(now).isoformat())


# --- a missed run -------------------------------------------------------------------------------------------

def record_attempt(conn: sqlite3.Connection, *, now: datetime) -> None:
    """Note that a check started (even one that then failed), so only a run that never happened counts as missed."""
    db.set_meta(conn, _LAST_ATTEMPT, _utc(now).isoformat())


def missed_run_alert(conn: sqlite3.Connection, *, now: datetime) -> FailureAlert | None:
    """An alert if no check was attempted for over `MISSED_AFTER`. None on the very first run."""
    last = db.get_meta(conn, _LAST_ATTEMPT)
    if last is None:
        return None
    since = datetime.fromisoformat(last)
    gap = _utc(now) - since
    if gap <= MISSED_AFTER:
        return None
    days = gap.days
    return FailureAlert(
        key="missed-run",
        title="Flight check was missed",
        message=(
            f"No daily check ran for about {days} day{'' if days == 1 else 's'} "
            f"(the last was {since.astimezone():%b %d}). If the Mac was off or asleep at 7:00, this run is "
            "catching up. If it keeps happening, check the schedule: launchctl list | grep flightoptimizer"
        ),
        priority=3, tags=("hourglass",),
    )
