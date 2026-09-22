from datetime import date

import pytest

from flight_optimizer import db
from flight_optimizer.windows import FLEXIBLE_WINDOW_DAYS, date_window, window_days, window_note

JAN9 = date(2027, 1, 9)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def trip(conn, **overrides):
    fields = dict(name="T", origin="RNO", destination="SJO", depart_date=JAN9, return_date=date(2027, 1, 16))
    trip_id = db.add_trip(conn, **{**fields, **overrides})
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


def test_the_owners_window_is_three_days_either_side():
    assert FLEXIBLE_WINDOW_DAYS == 3


def test_trips_are_exact_unless_marked_flexible(conn):
    assert trip(conn)["flexible"] == 0 and window_days(trip(conn, name="X", depart_date=date(2027, 2, 1))) == 0
    flexible = trip(conn, name="Flex", depart_date=date(2027, 3, 1), flexible=True)
    assert flexible["flexible"] == 1 and window_days(flexible) == 3


def test_a_window_reaches_the_same_number_of_days_each_way():
    assert date_window(JAN9, 3, today=date(2026, 9, 1)) == (date(2027, 1, 6), date(2027, 1, 12))


def test_a_zero_day_window_is_the_exact_date():
    assert date_window(JAN9, 0, today=date(2026, 9, 1)) == (JAN9, JAN9)


def test_a_window_never_starts_in_the_past():
    assert date_window(date(2026, 9, 22), 3, today=date(2026, 9, 21)) == (date(2026, 9, 21), date(2026, 9, 25))
    assert date_window(date(2026, 9, 21), 3, today=date(2026, 9, 21)) == (date(2026, 9, 21), date(2026, 9, 24))


def test_a_window_that_has_wholly_passed_is_nothing():
    assert date_window(date(2026, 9, 1), 3, today=date(2026, 9, 21)) is None
    assert date_window(date(2026, 9, 1), 0, today=date(2026, 9, 21)) is None


def test_the_note_is_empty_for_exact_trips_and_states_the_window_for_flexible_ones(conn):
    assert window_note(trip(conn)) == ""
    assert window_note(trip(conn, name="Flex", depart_date=date(2027, 3, 1), flexible=True)) == " (flexible: arrive up to 3 days early, return +/-3)"


# --- the database column -----------------------------------------------------------------------------------

def test_an_existing_database_gets_the_column_without_losing_trips(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            depart_date TEXT NOT NULL, return_date TEXT, active INTEGER NOT NULL DEFAULT 0,
            cabin TEXT NOT NULL DEFAULT 'business', max_miles INTEGER, max_cash_price REAL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        INSERT INTO trips (name, origin, destination, depart_date, return_date, active)
        VALUES ('Costa Rica', 'RNO', 'SJO', '2027-01-09', '2027-01-16', 1);
        """
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    db.init_schema(conn)
    db.init_schema(conn)   # safe to run again

    (row,) = conn.execute("SELECT * FROM trips").fetchall()
    assert row["name"] == "Costa Rica" and row["active"] == 1 and row["flexible"] == 0


def test_the_column_only_accepts_zero_or_one(conn):
    import sqlite3

    trip(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE trips SET flexible = 2")


def test_update_can_switch_a_trip_between_exact_and_flexible_and_leaves_it_alone_otherwise(conn):
    row = trip(conn)

    db.update_trip(conn, row["id"], flexible=True)
    assert conn.execute("SELECT flexible FROM trips").fetchone()[0] == 1
    db.update_trip(conn, row["id"], name="Renamed")
    assert conn.execute("SELECT flexible FROM trips").fetchone()[0] == 1
    db.update_trip(conn, row["id"], flexible=False)
    assert conn.execute("SELECT flexible FROM trips").fetchone()[0] == 0


def test_a_flexible_arrival_date_is_a_deadline():
    from flight_optimizer.windows import outbound_windows

    leaving, landing = outbound_windows(date(2026, 12, 22), 3, today=date(2026, 9, 22))

    assert landing == (date(2026, 12, 19), date(2026, 12, 22))  # up to 3 days early, never late
    assert leaving == (date(2026, 12, 18), date(2026, 12, 22))  # a day before the earliest landing


def test_an_exact_arrival_may_still_leave_the_day_before():
    from flight_optimizer.windows import outbound_windows

    assert outbound_windows(date(2027, 1, 9), 0, today=date(2026, 9, 22)) == (
        (date(2027, 1, 8), date(2027, 1, 9)), (date(2027, 1, 9), date(2027, 1, 9)),
    )
