import sqlite3
from datetime import date

import pytest

from flight_optimizer import db


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def new_trip(conn, **overrides):
    fields = dict(
        name="Europe",
        origin="jfk",
        destination="lhr",
        outbound_date=date(2026, 11, 1),
        return_date=date(2026, 11, 10),
    )
    return db.add_trip(conn, **{**fields, **overrides})


def test_init_schema_is_idempotent(conn):
    db.init_schema(conn)
    db.init_schema(conn)


def test_new_trips_are_inactive_by_default(conn):
    new_trip(conn)

    assert db.active_trips(conn) == []


def test_trip_fields_are_normalized(conn):
    trip_id = new_trip(conn)

    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    assert (row["origin"], row["destination"]) == ("JFK", "LHR")
    assert (row["outbound_date"], row["return_date"]) == ("2026-11-01", "2026-11-10")


def test_a_trip_has_no_cabin_or_price_limit_columns(conn):
    # Every trip is priced in every cabin, and cash targets (`trip target`) replaced the price limits.
    new_trip(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trips)")}
    assert not columns & {"cabin", "max_miles", "max_cash_price"}
    assert {"name", "origin", "destination", "outbound_date", "return_date", "active", "flexible"} <= columns


def test_one_way_trip_has_no_return_date(conn):
    trip_id = new_trip(conn, return_date=None)

    row = conn.execute("SELECT return_date FROM trips WHERE id = ?", (trip_id,)).fetchone()
    assert row["return_date"] is None


def test_active_trips_only_returns_active_ones(conn):
    shelved = new_trip(conn, name="Shelved")
    booked = new_trip(conn, name="Booked", active=True)
    watching = new_trip(conn, name="Watching")
    db.set_trip_active(conn, watching, True)

    assert [t["id"] for t in db.active_trips(conn)] == [booked, watching]

    db.set_trip_active(conn, booked, False)
    assert [t["id"] for t in db.active_trips(conn)] == [watching]
    assert shelved not in [t["id"] for t in db.active_trips(conn)]


def test_gateways_are_normalized_deduped_and_replaced(conn):
    trip_id = new_trip(conn)

    db.set_gateways(conn, trip_id, [" lax", "SFO", "LAX"])
    assert db.get_gateways(conn, trip_id) == ["LAX", "SFO"]

    db.set_gateways(conn, trip_id, ["DEN"])
    assert db.get_gateways(conn, trip_id) == ["DEN"]


def test_gateways_reject_bad_codes_and_the_trips_own_airports(conn):
    trip_id = new_trip(conn)  # JFK -> LHR

    with pytest.raises(ValueError):
        db.set_gateways(conn, trip_id, ["LOS ANGELES"])
    with pytest.raises(ValueError):
        db.set_gateways(conn, trip_id, ["JFK"])
    with pytest.raises(ValueError):
        db.set_gateways(conn, trip_id, ["LHR"])
    with pytest.raises(LookupError):
        db.set_gateways(conn, 999, ["LAX"])
    assert db.get_gateways(conn, trip_id) == []


def test_deleting_a_trip_removes_its_gateways(conn):
    trip_id = new_trip(conn)
    db.set_gateways(conn, trip_id, ["LAX"])

    conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))

    assert conn.execute("SELECT COUNT(*) FROM trip_gateways").fetchone()[0] == 0


def test_set_trip_active_unknown_trip_raises(conn):
    with pytest.raises(LookupError):
        db.set_trip_active(conn, 999, True)


def test_balances_default_to_zero_for_every_program(conn):
    assert db.get_balances(conn) == dict.fromkeys(db.PROGRAMS, 0)


def test_set_balance_inserts_then_updates(conn):
    db.set_balance(conn, "united", 50_000)
    db.set_balance(conn, "united", 62_500)

    assert db.get_balances(conn)["united"] == 62_500
    assert conn.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 1


def test_set_balance_rejects_unknown_program_and_negative(conn):
    with pytest.raises(ValueError):
        db.set_balance(conn, "not_a_program", 1)
    with pytest.raises(ValueError):
        db.set_balance(conn, "delta", -1)


def test_last_alert_returns_most_recent_for_matching_key(conn):
    trip_id = new_trip(conn)
    assert db.last_alert(conn, trip_id, "british|2026-11-01|J") is None

    db.record_alert(conn, trip_id=trip_id, alert_type="award", dedupe_key="british|2026-11-01|J", miles=99_000)
    db.record_alert(conn, trip_id=trip_id, alert_type="award", dedupe_key="british|2026-11-01|J", miles=88_000)
    db.record_alert(conn, trip_id=trip_id, alert_type="award", dedupe_key="other", miles=1)

    assert db.last_alert(conn, trip_id, "british|2026-11-01|J")["miles"] == 88_000


def test_deleting_a_trip_removes_its_alert_history(conn):
    trip_id = new_trip(conn)
    db.record_alert(conn, trip_id=trip_id, alert_type="award", dedupe_key="k")

    conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))

    assert conn.execute("SELECT COUNT(*) FROM alert_history").fetchone()[0] == 0


def test_alert_for_missing_trip_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.record_alert(conn, trip_id=999, alert_type="award", dedupe_key="k")


# --- retiring columns nothing reads ------------------------------------------------------------------------------

OLD_TRIPS = """
CREATE TABLE trips (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
    depart_date TEXT NOT NULL, return_date TEXT, active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    cabin TEXT NOT NULL DEFAULT 'business' CHECK (cabin IN ('economy', 'premium_economy', 'business', 'first')),
    flexible INTEGER NOT NULL DEFAULT 0 CHECK (flexible IN (0, 1)),
    max_miles INTEGER CHECK (max_miles IS NULL OR max_miles > 0),
    max_cash_price REAL CHECK (max_cash_price IS NULL OR max_cash_price > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO trips (name, origin, destination, depart_date, return_date, active, cabin, flexible, max_miles)
VALUES ('Costa Rica', 'RNO', 'SJO', '2027-01-09', '2027-01-16', 1, 'business', 1, 90000);
"""


def old_database(tmp_path):
    path = tmp_path / "trips.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_TRIPS)
    old.commit()
    old.close()
    return path


def test_an_older_database_loses_the_retired_columns_and_keeps_its_trips(tmp_path):
    path = old_database(tmp_path)
    conn = db.connect(path)

    db.init_schema(conn)

    (row,) = conn.execute("SELECT * FROM trips").fetchall()
    assert row["name"] == "Costa Rica" and row["active"] == 1 and row["flexible"] == 1 and row["outbound_date"] == "2027-01-09"
    assert not {"cabin", "max_miles", "max_cash_price"} & set(row.keys())


def test_the_old_database_is_backed_up_first_with_everything_it_had(tmp_path):
    path = old_database(tmp_path)
    conn = db.connect(path)

    db.init_schema(conn)

    (backup,) = tmp_path.glob("trips-backup-*.db")
    assert backup.name == f"trips-backup-{date.today():%Y%m%d}.db"       # a *.db name: git ignores it
    saved = sqlite3.connect(backup).execute("SELECT cabin, max_miles FROM trips").fetchone()
    assert saved == ("business", 90_000)


def test_running_it_again_changes_nothing_and_keeps_the_first_backup(tmp_path):
    path = old_database(tmp_path)
    conn = db.connect(path)
    db.init_schema(conn)
    (backup,) = tmp_path.glob("trips-backup-*.db")
    backup_bytes = backup.read_bytes()

    db.init_schema(conn)
    db.init_schema(db.connect(path))

    assert list(tmp_path.glob("trips-backup-*.db")) == [backup] and backup.read_bytes() == backup_bytes


def test_an_up_to_date_database_makes_no_backup(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")

    db.init_schema(conn)
    db.init_schema(conn)

    assert list(tmp_path.glob("*backup*")) == []


def test_an_in_memory_database_makes_no_backup_and_still_migrates(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OLD_TRIPS)

    db.init_schema(conn)

    assert not {"cabin", "max_miles", "max_cash_price"} & {row[1] for row in conn.execute("PRAGMA table_info(trips)")}
    assert list(tmp_path.iterdir()) == []


def test_other_tables_survive_the_migration(tmp_path):
    path = old_database(tmp_path)
    conn = db.connect(path)
    db.init_schema(conn)
    db.set_gateways(conn, 1, ["LAX"])
    db.record_alert(conn, trip_id=1, alert_type="award", dedupe_key="k", miles=1)
    db.set_balance(conn, "delta", 5)

    db.init_schema(db.connect(path))

    conn = db.connect(path)
    assert db.get_gateways(conn, 1) == ["LAX"] and db.last_alert(conn, 1, "k") is not None and db.get_balances(conn)["delta"] == 5
