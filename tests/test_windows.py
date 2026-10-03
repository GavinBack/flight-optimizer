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
    fields = dict(name="T", origin="RNO", destination="SJO", outbound_date=JAN9, return_date=date(2027, 1, 16))
    trip_id = db.add_trip(conn, **{**fields, **overrides})
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


def test_the_owners_window_is_three_days_either_side():
    assert FLEXIBLE_WINDOW_DAYS == 3


def test_trips_are_exact_unless_marked_flexible(conn):
    assert trip(conn)["outbound_flexible"] == 0 and window_days(trip(conn, name="X", outbound_date=date(2027, 2, 1)), "outbound") == 0
    flexible = trip(conn, name="Flex", outbound_date=date(2027, 3, 1), outbound_flexible=True, return_flexible=True)
    assert window_days(flexible, "outbound") == 3 and window_days(flexible, "return") == 3


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
    assert window_note(trip(conn, name="Flex", outbound_date=date(2027, 3, 1), outbound_flexible=True, return_flexible=True)) == " (flexible: +/-3 days)"
    assert window_note(trip(conn, name="Arrive", outbound_date=date(2027, 4, 1), outbound_flexible=True, return_flexible=True, date_anchor="arrive")) == (
        " (flexible: +/-3 days)"
    )


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
    assert row["name"] == "Costa Rica" and row["active"] == 1 and row["outbound_flexible"] == 0
    # saved when every outbound date meant the day to land: it keeps that meaning
    assert row["date_anchor"] == "arrive"
    assert row["outbound_date"] == "2027-01-09" and "depart_date" not in row.keys()  # the column was renamed
    assert list(tmp_path.glob("old-backup-*.db"))  # backed up before the meaning was written down


def test_the_anchor_migration_runs_once_and_new_trips_default_to_departure(tmp_path):
    import sqlite3

    path = tmp_path / "sept.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
            depart_date TEXT NOT NULL, return_date TEXT, active INTEGER NOT NULL DEFAULT 0,
            flexible INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        INSERT INTO trips (name, origin, destination, depart_date, return_date, active, flexible)
        VALUES ('Kelowna', 'RNO', 'YLW', '2026-12-22', '2026-12-28', 1, 1);
        """
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    db.init_schema(conn)
    db.update_trip(conn, 1, date_anchor="depart")  # the owner switches it back
    new_id = db.add_trip(conn, name="New", origin="RNO", destination="SJO", outbound_date=date(2027, 5, 1))
    db.init_schema(conn)  # a later start must not flip it again

    anchors = dict(conn.execute("SELECT id, date_anchor FROM trips").fetchall())
    assert anchors == {1: "depart", new_id: "depart"}


def test_an_unknown_anchor_is_refused(conn):
    with pytest.raises(ValueError, match="date_anchor"):
        db.add_trip(conn, name="X", origin="RNO", destination="SJO", outbound_date=JAN9, date_anchor="land")


def test_the_column_only_accepts_zero_or_one(conn):
    import sqlite3

    trip(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE trips SET outbound_flexible = 2")


def test_update_can_switch_a_trip_between_exact_and_flexible_and_leaves_it_alone_otherwise(conn):
    row = trip(conn)

    db.update_trip(conn, row["id"], outbound_flexible=True, return_flexible=True)
    assert tuple(conn.execute("SELECT outbound_flexible, return_flexible FROM trips").fetchone()) == (1, 1)
    db.update_trip(conn, row["id"], name="Renamed")
    assert tuple(conn.execute("SELECT outbound_flexible, return_flexible FROM trips").fetchone()) == (1, 1)
    db.update_trip(conn, row["id"], outbound_flexible=False, return_flexible=False)
    assert tuple(conn.execute("SELECT outbound_flexible, return_flexible FROM trips").fetchone()) == (0, 0)


def test_a_flexible_arrival_date_lands_three_days_either_side():
    from flight_optimizer.windows import outbound_windows

    leaving, landing = outbound_windows(date(2026, 12, 22), 3, today=date(2026, 9, 22))

    assert landing == (date(2026, 12, 19), date(2026, 12, 25))
    assert leaving == (date(2026, 12, 18), date(2026, 12, 25))  # from a day before the earliest landing


def test_an_exact_arrival_may_still_leave_the_day_before():
    from flight_optimizer.windows import outbound_windows

    assert outbound_windows(date(2027, 1, 9), 0, today=date(2026, 9, 22)) == (
        (date(2027, 1, 8), date(2027, 1, 9)), (date(2027, 1, 9), date(2027, 1, 9)),
    )


def test_the_note_and_label_say_which_date_is_flexible(conn):
    from flight_optimizer.windows import flexibility_label

    out = trip(conn, name="Out", outbound_flexible=True)
    back = trip(conn, name="Back", outbound_date=date(2027, 5, 1), return_date=date(2027, 5, 8), return_flexible=True)
    both = trip(conn, name="Both", outbound_date=date(2027, 6, 1), return_date=date(2027, 6, 8),
                outbound_flexible=True, return_flexible=True)

    assert window_note(out) == " (flexible: outbound +/-3 days, return exact)"
    assert window_note(back) == " (flexible: return +/-3 days, outbound exact)"
    assert window_note(both) == " (flexible: +/-3 days)"
    assert [flexibility_label(t) for t in (trip(conn, name="None", outbound_date=date(2027, 7, 1)), out, back, both)] == [
        "exact", "out +/-3", "ret +/-3", "+/-3 days",
    ]
