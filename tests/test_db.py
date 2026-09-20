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
        depart_date=date(2026, 11, 1),
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
    assert (row["depart_date"], row["return_date"]) == ("2026-11-01", "2026-11-10")


def test_trip_defaults_to_business_with_no_limits(conn):
    trip_id = new_trip(conn)

    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    assert row["cabin"] == "business"
    assert row["max_miles"] is None
    assert row["max_cash_price"] is None


def test_trip_stores_cabin_and_limits(conn):
    trip_id = new_trip(conn, cabin="first", max_miles=80_000, max_cash_price=1500.0)

    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    assert (row["cabin"], row["max_miles"], row["max_cash_price"]) == ("first", 80_000, 1500.0)


def test_trip_rejects_unknown_cabin_and_nonpositive_limits(conn):
    with pytest.raises(ValueError):
        new_trip(conn, cabin="steerage")
    with pytest.raises(sqlite3.IntegrityError):
        new_trip(conn, max_miles=0)
    with pytest.raises(sqlite3.IntegrityError):
        new_trip(conn, max_cash_price=-5)


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
