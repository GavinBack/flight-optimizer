"""Adding and describing trips, with the validation the database alone does not do."""

import re
import sqlite3
from datetime import date

from flight_optimizer import db
from flight_optimizer.windows import flexibility_label, is_arrival


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
    outbound_date: date,
    return_date: date | None = None,
    gateways: list[str] | tuple[str, ...] = (),
    active: bool = True,
    outbound_flexible: bool = False,
    return_flexible: bool = False,
    date_anchor: str = "depart",
) -> int:
    """Validate everything first, then save the trip and its gateways together."""
    if not name.strip():
        raise ValueError("A trip needs a name.")
    origin, destination = airport(origin), airport(destination)
    if origin == destination:
        raise ValueError("The origin and destination are the same airport.")
    if return_date is not None and return_date < outbound_date:
        raise ValueError("The return date is before the outbound date.")
    if return_flexible and return_date is None:
        raise ValueError("A one-way trip has no return date to make flexible.")
    hubs = list(dict.fromkeys(airport(g) for g in gateways))
    for hub in hubs:
        if hub in (origin, destination):
            raise ValueError(f"Gateway {hub} is the trip's own origin or destination.")

    duplicate = conn.execute(
        "SELECT id FROM trips WHERE origin = ? AND destination = ? AND outbound_date = ? AND return_date IS ?",
        (origin, destination, outbound_date.isoformat(), return_date.isoformat() if return_date else None),
    ).fetchone()
    if duplicate is not None:
        raise ValueError(f"A trip with the same route and dates already exists (trip {duplicate['id']}).")

    trip_id = db.add_trip(
        conn, name=name.strip(), origin=origin, destination=destination, outbound_date=outbound_date,
        return_date=return_date, active=active, outbound_flexible=outbound_flexible,
        return_flexible=return_flexible, date_anchor=date_anchor,
    )
    if hubs:
        db.set_gateways(conn, trip_id, hubs)
    return trip_id


def format_trips(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM trips ORDER BY id").fetchall()
    if not rows:
        return "No trips yet. Add one with: python -m flight_optimizer trip add"
    lines = [f"{'ID':<4}{'Active':<8}{'Trip':<20}{'Route':<14}{'Dates':<35}{'Flexible':<11}Gateways"]
    for trip in rows:
        outbound = date.fromisoformat(trip["outbound_date"])
        dates = f"{'arrive' if is_arrival(trip) else 'leave'} {outbound:%b %d %Y}"
        if trip["return_date"]:
            dates += f", return {date.fromisoformat(trip['return_date']):%b %d}"
        else:
            dates += " (one way)"
        gateways = ", ".join(db.get_gateways(conn, trip["id"])) or "none"
        lines.append(
            f"{trip['id']:<4}{'yes' if trip['active'] else 'no':<8}{trip['name'][:19]:<20}"
            f"{trip['origin'] + ' > ' + trip['destination']:<14}{dates:<35}"
            f"{flexibility_label(trip):<11}{gateways}"
        )
    return "\n".join(lines)
