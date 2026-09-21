"""Adding and describing trips, with the validation the database alone does not do."""

import re
import sqlite3
from datetime import date

from flight_optimizer import db
from flight_optimizer.windows import FLEXIBLE_WINDOW_DAYS


def airport(code: str) -> str:
    """A normalized 3-letter airport code, or ValueError."""
    cleaned = code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", cleaned):
        raise ValueError(f"{code!r} is not a 3-letter airport code.")
    return cleaned


def parse_date(text: str, what: str = "date") -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"The {what} {text!r} should look like 2026-12-22.") from None


def add_trip(
    conn: sqlite3.Connection,
    *,
    name: str,
    origin: str,
    destination: str,
    depart_date: date,
    return_date: date | None = None,
    gateways: list[str] | tuple[str, ...] = (),
    active: bool = True,
    flexible: bool = False,
) -> int:
    """Validate everything first, then save the trip and its gateways together."""
    if not name.strip():
        raise ValueError("A trip needs a name.")
    origin, destination = airport(origin), airport(destination)
    if origin == destination:
        raise ValueError("The origin and destination are the same airport.")
    if return_date is not None and return_date < depart_date:
        raise ValueError("The return date is before the departure date.")
    hubs = list(dict.fromkeys(airport(g) for g in gateways))
    for hub in hubs:
        if hub in (origin, destination):
            raise ValueError(f"Gateway {hub} is the trip's own origin or destination.")

    duplicate = conn.execute(
        "SELECT id FROM trips WHERE origin = ? AND destination = ? AND depart_date = ? AND return_date IS ?",
        (origin, destination, depart_date.isoformat(), return_date.isoformat() if return_date else None),
    ).fetchone()
    if duplicate is not None:
        raise ValueError(f"A trip with the same route and dates already exists (trip {duplicate['id']}).")

    trip_id = db.add_trip(
        conn, name=name.strip(), origin=origin, destination=destination, depart_date=depart_date,
        return_date=return_date, active=active, flexible=flexible,
    )
    if hubs:
        db.set_gateways(conn, trip_id, hubs)
    return trip_id


def format_trips(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM trips ORDER BY id").fetchall()
    if not rows:
        return "No trips yet. Add one with: python -m flight_optimizer trip add"
    lines = [f"{'ID':<4}{'Active':<8}{'Trip':<20}{'Route':<14}{'Dates':<26}{'Flexible':<10}Gateways"]
    for trip in rows:
        depart = date.fromisoformat(trip["depart_date"])
        dates = f"{depart:%b %d %Y}"
        if trip["return_date"]:
            dates += f" - {date.fromisoformat(trip['return_date']):%b %d}"
        else:
            dates += " (one way)"
        gateways = ", ".join(db.get_gateways(conn, trip["id"])) or "none"
        lines.append(
            f"{trip['id']:<4}{'yes' if trip['active'] else 'no':<8}{trip['name'][:19]:<20}"
            f"{trip['origin'] + ' > ' + trip['destination']:<14}{dates:<26}"
            f"{('+/-' + str(FLEXIBLE_WINDOW_DAYS) + ' days') if trip['flexible'] else 'exact':<10}{gateways}"
        )
    return "\n".join(lines)
