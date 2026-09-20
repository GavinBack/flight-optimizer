"""Decide whether a trip deserves an alert, and write it.

An alert is a short summary and an urgency line for the owner's phone. The
step-by-step booking plan lives in the app (`plans.py`), to be read at a laptop.
Alerts never include balances.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flight_optimizer import db
from flight_optimizer.affordability import TripOption
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.connections import ConnectionClass
from flight_optimizer.savings import estimate_value

RE_ALERT_DAYS = 7  # don't repeat the same option within this window unless it got cheaper
URGENT_SEATS = 2  # at most this many seats on any leg: act today
SOON_SEATS = 5  # at most this many: act within a few days

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Urgency:
    """A guide from seat counts, not a prediction: award space can vanish at any time."""

    label: str  # ACT TODAY / ACT WITHIN A FEW DAYS / NO RUSH
    reason: str
    priority: int  # ntfy priority, 1 (min) to 5 (max)


@dataclass(frozen=True)
class Alert:
    trip_id: int
    option: TripOption
    urgency: Urgency
    key: str  # identifies this exact option, for de-duplication
    title: str
    message: str
    click: str | None  # opens when the notification is tapped
    priority: int
    tags: tuple[str, ...]


def option_legs(option: TripOption):
    return [leg for it in option.itineraries for leg in it.legs]


def leg_name(leg) -> str:
    return f"{leg.origin}>{leg.destination} {leg.source}"


def assess_urgency(option: TripOption) -> Urgency:
    scarcest = min(option_legs(option), key=lambda leg: leg.remaining_seats)
    seats = scarcest.remaining_seats
    plural = "" if seats == 1 else "s"
    if seats <= URGENT_SEATS:
        return Urgency("ACT TODAY", f"{leg_name(scarcest)} has {seats} seat{plural} left", 4)
    if seats <= SOON_SEATS:
        return Urgency(
            "ACT WITHIN A FEW DAYS", f"{leg_name(scarcest)} has {seats} seats left", 3
        )
    return Urgency("NO RUSH", f"every leg has {seats} or more seats", 2)


def option_key(option: TripOption) -> str:
    """Stable identity of an option: which flights, on which program and date."""
    return "|".join(
        f"{leg.source}:{leg.origin}>{leg.destination}:{leg.date.isoformat()}:{leg.flight_numbers}"
        for leg in option_legs(option)
    )


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def heads_up(option: TripOption) -> list[str]:
    notes = []
    for it in option.itineraries:
        hub = it.via or ""
        if it.connection_class is ConnectionClass.TIGHT:
            notes.append(f"tight {_hm(it.connection_minutes)} connection at {hub}")
        elif it.connection_class is ConnectionClass.OVERNIGHT:
            notes.append(f"overnight at {hub}")
    if option.self_transfers:
        notes.append("separate bookings: bags re-checked, no missed-connection cover")
    return notes


def oldest_data(option: TripOption) -> str | None:
    dates = [leg.updated_at[:10] for leg in option_legs(option) if leg.updated_at]
    return min(dates) if dates else None


def _dates(trip: sqlite3.Row) -> str:
    depart = date.fromisoformat(trip["depart_date"])
    text = f"{depart:%b %d}"
    if trip["return_date"]:
        text += f" - {date.fromisoformat(trip['return_date']):%b %d}"
    return text


def _changed_legs(option: TripOption, other: TripOption) -> list[str]:
    """Which legs of `other` differ from `option` (different flights, date or program)."""
    ours, theirs = option_legs(option), option_legs(other)
    if len(ours) != len(theirs):
        return []
    return [
        leg_name(t)
        for o, t in zip(ours, theirs)
        if (o.source, o.date, o.flight_numbers) != (t.source, t.date, t.flight_numbers)
    ]


def build_alert(analysis: TripAnalysis, option: TripOption, *, fallback: TripOption | None) -> Alert:
    trip = analysis.trip
    urgency = assess_urgency(option)

    value_line = f"{option.total_miles:,} miles"
    if analysis.baseline is not None:
        value = estimate_value(option, analysis.baseline)
        if value.cents_per_mile_vs_cabin is not None:
            value_line += (
                f" ({value.cents_per_mile_vs_cabin:.1f} c/mile vs "
                f"${value.cabin_cash:,} {trip['cabin'].replace('_', ' ')} cash)"
            )

    lines = [
        f"{trip['origin']} > {trip['destination']}  {_dates(trip)}",
        value_line,
        f"{urgency.label}: {urgency.reason}",
    ]
    if fallback is not None:
        extra = fallback.total_miles - option.total_miles
        changed = _changed_legs(option, fallback)
        detail = f", changes {', '.join(changed)}" if changed else ""
        lines.append(f"If it goes: next best is {fallback.total_miles:,} miles ({extra:+,}){detail}")
    else:
        lines.append("If it goes: no other affordable option right now")
    notes = heads_up(option)
    if notes:
        lines.append("Note: " + "; ".join(notes))
    oldest = oldest_data(option)
    lines.append(
        (f"Data as of {oldest}. " if oldest else "")
        + f"Full steps: python -m flight_optimizer plan {trip['id']}"
    )

    click = next((leg.booking_url for leg in option_legs(option) if leg.booking_url), None)
    return Alert(
        trip_id=trip["id"],
        option=option,
        urgency=urgency,
        key=option_key(option),
        title=f"{trip['name']}: {option.total_miles:,} miles",
        message="\n".join(lines),
        click=click,
        priority=urgency.priority,
        tags=("airplane",) + (("warning",) if urgency.priority >= 4 else ()),
    )


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def decide_alert(
    conn: sqlite3.Connection, analysis: TripAnalysis, *, now: datetime | None = None
) -> Alert | None:
    """An alert for the trip's best affordable option, or None if there's nothing new.

    Suppressed when the same option was alerted within `RE_ALERT_DAYS` and hasn't
    got cheaper since (the README's re-alert rule).
    """
    affordable = analysis.affordable
    if not affordable:
        return None
    best = affordable[0]
    key = option_key(best)

    last = db.last_alert(conn, analysis.trip["id"], key)
    if last is not None:
        # A naive `now` is read as local time; astimezone() makes any input UTC.
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        recent = now - _parse_time(last["created_at"]) < timedelta(days=RE_ALERT_DAYS)
        cheaper = last["miles"] is not None and best.total_miles < last["miles"]
        if recent and not cheaper:
            return None

    fallback = next((o for o in affordable[1:] if option_key(o) != key), None)
    return build_alert(analysis, best, fallback=fallback)


def record_sent(conn: sqlite3.Connection, alert: Alert, plan_path: str | None = None) -> None:
    """Remember an alert that was actually delivered."""
    db.record_alert(
        conn,
        trip_id=alert.trip_id,
        alert_type="award",
        dedupe_key=alert.key,
        miles=alert.option.total_miles,
        details=plan_path,
    )
