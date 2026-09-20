"""SQLite storage: trips being watched, program balances, and alert history."""

import re
import sqlite3
from datetime import date
from pathlib import Path

# Programs whose balances are tracked (README, "Inputs", plus Capital One transfer
# partners and other programs that appear in Seats.aero results).
PROGRAMS = (
    "capital_one",
    "delta",
    "american",
    "united",
    "qantas",
    "atmos",
    "southwest",
    "aeroplan",
    "lifemiles",
    "flyingblue",
    "british",
    "virginatlantic",
    "jetblue",
)

CABINS = ("economy", "premium_economy", "business", "first")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    -- Gates the daily check and every alert for this trip. Off by default.
    active      INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    cabin       TEXT NOT NULL DEFAULT 'business'
                CHECK (cabin IN ('economy', 'premium_economy', 'business', 'first')),
    -- NULL means no limit.
    max_miles      INTEGER CHECK (max_miles IS NULL OR max_miles > 0),
    max_cash_price REAL CHECK (max_cash_price IS NULL OR max_cash_price > 0),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Hub airports to route through when origin and destination have no direct award route.
CREATE TABLE IF NOT EXISTS trip_gateways (
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    airport TEXT NOT NULL,
    PRIMARY KEY (trip_id, airport)
);

-- Cached cash-fare searches. SerpApi's free plan is 250 searches a month, so a repeat
-- run must not re-search. Every fare is stored so baseline filters can change freely.
CREATE TABLE IF NOT EXISTS cash_fare_cache (
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    travel_date  TEXT NOT NULL,
    cabin        TEXT NOT NULL,
    fares_json   TEXT NOT NULL,
    lowest_price INTEGER,
    typical_low  INTEGER,
    typical_high INTEGER,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (origin, destination, travel_date, cabin)
);

-- Seats.aero flight details, keyed by the search row's own UpdatedAt stamp. That stamp
-- equals every flight entry's UpdatedAt (checked on live data), so an unchanged stamp
-- means unchanged flights: safe to reuse with no time limit.
CREATE TABLE IF NOT EXISTS trips_cache (
    availability_id TEXT NOT NULL,
    version         TEXT NOT NULL,
    details_json    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (availability_id, version)
);

-- Seats.aero search results have no version stamp, so these expire on a short timer.
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key    TEXT PRIMARY KEY,
    results_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balances (
    program    TEXT PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS alert_history (
    id         INTEGER PRIMARY KEY,
    trip_id    INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    -- Identifies "the same thing" (e.g. source|date|cabin) for re-alert suppression.
    dedupe_key TEXT NOT NULL,
    miles      INTEGER,
    cash_price REAL,
    details    TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_alert_history_lookup
    ON alert_history (trip_id, dedupe_key, id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing. Safe to call on every start."""
    with conn:
        conn.executescript(SCHEMA)


def add_trip(
    conn: sqlite3.Connection,
    *,
    name: str,
    origin: str,
    destination: str,
    depart_date: date,
    return_date: date | None = None,
    active: bool = False,
    cabin: str = "business",
    max_miles: int | None = None,
    max_cash_price: float | None = None,
) -> int:
    """Save a trip. `max_miles` / `max_cash_price` of None mean no limit."""
    if cabin not in CABINS:
        raise ValueError(f"Unknown cabin {cabin!r}; expected one of {CABINS}")
    with conn:
        cur = conn.execute(
            "INSERT INTO trips (name, origin, destination, depart_date, return_date,"
            " active, cabin, max_miles, max_cash_price)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                origin.upper(),
                destination.upper(),
                depart_date.isoformat(),
                return_date.isoformat() if return_date else None,
                int(active),
                cabin,
                max_miles,
                max_cash_price,
            ),
        )
    return cur.lastrowid


def set_gateways(conn: sqlite3.Connection, trip_id: int, airports: list[str]) -> None:
    """Replace the trip's gateway airports."""
    codes = list(dict.fromkeys(a.strip().upper() for a in airports))
    for code in codes:
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError(f"{code!r} is not a 3-letter airport code")

    trip = conn.execute(
        "SELECT origin, destination FROM trips WHERE id = ?", (trip_id,)
    ).fetchone()
    if trip is None:
        raise LookupError(f"No trip with id {trip_id}")
    for code in codes:
        if code in (trip["origin"], trip["destination"]):
            raise ValueError(f"Gateway {code} is the trip's own origin or destination")

    with conn:
        conn.execute("DELETE FROM trip_gateways WHERE trip_id = ?", (trip_id,))
        conn.executemany(
            "INSERT INTO trip_gateways (trip_id, airport) VALUES (?, ?)",
            [(trip_id, code) for code in codes],
        )


def get_gateways(conn: sqlite3.Connection, trip_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT airport FROM trip_gateways WHERE trip_id = ? ORDER BY airport", (trip_id,)
    )
    return [row["airport"] for row in rows]


def set_trip_active(conn: sqlite3.Connection, trip_id: int, active: bool) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE trips SET active = ? WHERE id = ?", (int(active), trip_id)
        )
    if cur.rowcount == 0:
        raise LookupError(f"No trip with id {trip_id}")


def active_trips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The only trips the daily run and alerts may act on."""
    return conn.execute("SELECT * FROM trips WHERE active = 1 ORDER BY id").fetchall()


def save_cash_fares(
    conn: sqlite3.Connection,
    *,
    origin: str,
    destination: str,
    travel_date: date,
    cabin: str,
    fares_json: str,
    lowest_price: int | None,
    typical_low: int | None,
    typical_high: int | None,
    fetched_at: str,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO cash_fare_cache (origin, destination, travel_date, cabin, fares_json,"
            " lowest_price, typical_low, typical_high, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(origin, destination, travel_date, cabin) DO UPDATE SET"
            " fares_json = excluded.fares_json, lowest_price = excluded.lowest_price,"
            " typical_low = excluded.typical_low, typical_high = excluded.typical_high,"
            " fetched_at = excluded.fetched_at",
            (origin.upper(), destination.upper(), travel_date.isoformat(), cabin,
             fares_json, lowest_price, typical_low, typical_high, fetched_at),
        )


def get_cash_fares(
    conn: sqlite3.Connection, origin: str, destination: str, travel_date: date, cabin: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cash_fare_cache"
        " WHERE origin = ? AND destination = ? AND travel_date = ? AND cabin = ?",
        (origin.upper(), destination.upper(), travel_date.isoformat(), cabin),
    ).fetchone()


def get_trips_cache(
    conn: sqlite3.Connection, availability_id: str, version: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM trips_cache WHERE availability_id = ? AND version = ?",
        (availability_id, version),
    ).fetchone()


def save_trips_cache(
    conn: sqlite3.Connection, availability_id: str, version: str, details_json: str, fetched_at: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO trips_cache (availability_id, version, details_json, fetched_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(availability_id, version) DO UPDATE SET"
            " details_json = excluded.details_json, fetched_at = excluded.fetched_at",
            (availability_id, version, details_json, fetched_at),
        )


def get_search_cache(conn: sqlite3.Connection, cache_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM search_cache WHERE cache_key = ?", (cache_key,)).fetchone()


def save_search_cache(
    conn: sqlite3.Connection, cache_key: str, results_json: str, fetched_at: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO search_cache (cache_key, results_json, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT(cache_key) DO UPDATE SET"
            " results_json = excluded.results_json, fetched_at = excluded.fetched_at",
            (cache_key, results_json, fetched_at),
        )


def prune_seats_cache(conn: sqlite3.Connection, *, trips_before: str, search_before: str) -> None:
    """Drop entries fetched before the given UTC timestamps (ISO strings sort correctly)."""
    with conn:
        conn.execute("DELETE FROM trips_cache WHERE fetched_at < ?", (trips_before,))
        conn.execute("DELETE FROM search_cache WHERE fetched_at < ?", (search_before,))


def set_balance(conn: sqlite3.Connection, program: str, balance: int) -> None:
    if program not in PROGRAMS:
        raise ValueError(f"Unknown program {program!r}; expected one of {PROGRAMS}")
    if balance < 0:
        raise ValueError("Balance cannot be negative")
    with conn:
        conn.execute(
            "INSERT INTO balances (program, balance) VALUES (?, ?)"
            " ON CONFLICT(program) DO UPDATE SET"
            " balance = excluded.balance,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (program, balance),
        )


def balance_updates(conn: sqlite3.Connection) -> dict[str, str]:
    """When each balance was last set (UTC ISO). Programs never set are absent."""
    return {row["program"]: row["updated_at"] for row in conn.execute("SELECT program, updated_at FROM balances")}


def get_balances(conn: sqlite3.Connection) -> dict[str, int]:
    """Every tracked program, defaulting to 0 if never set."""
    balances = dict.fromkeys(PROGRAMS, 0)
    for row in conn.execute("SELECT program, balance FROM balances"):
        balances[row["program"]] = row["balance"]
    return balances


def record_alert(
    conn: sqlite3.Connection,
    *,
    trip_id: int,
    alert_type: str,
    dedupe_key: str,
    miles: int | None = None,
    cash_price: float | None = None,
    details: str | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO alert_history"
            " (trip_id, alert_type, dedupe_key, miles, cash_price, details)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (trip_id, alert_type, dedupe_key, miles, cash_price, details),
        )
    return cur.lastrowid


def last_alert(
    conn: sqlite3.Connection, trip_id: int, dedupe_key: str
) -> sqlite3.Row | None:
    """Most recent alert for this trip and key, for the re-alert suppression rule."""
    return conn.execute(
        "SELECT * FROM alert_history WHERE trip_id = ? AND dedupe_key = ?"
        " ORDER BY id DESC LIMIT 1",
        (trip_id, dedupe_key),
    ).fetchone()
