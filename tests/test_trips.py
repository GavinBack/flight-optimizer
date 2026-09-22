from datetime import date

import pytest

from flight_optimizer import db
from flight_optimizer.trips import add_trip, airport, format_trips, parse_date

DEC22, DEC28 = date(2026, 12, 22), date(2026, 12, 28)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def kelowna(conn, **overrides):
    fields = dict(name="Kelowna", origin="rno", destination="ylw", depart_date=DEC22, return_date=DEC28,
                  gateways=["sea", "LAX", "phx"])
    return add_trip(conn, **{**fields, **overrides})


def row(conn, trip_id):
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


# --- airport codes and dates -----------------------------------------------------------------------------

@pytest.mark.parametrize("typed, expected", [("rno", "RNO"), (" ylw ", "YLW"), ("SeA", "SEA")])
def test_airport_codes_are_normalized(typed, expected):
    assert airport(typed) == expected


@pytest.mark.parametrize("bad", ["", "RN", "RENO", "R2O", "12", "RN O"])
def test_bad_airport_codes_are_rejected(bad):
    with pytest.raises(ValueError, match="3-letter airport code"):
        airport(bad)


def test_parse_date_accepts_iso_dates_and_names_the_field_when_it_fails():
    assert parse_date(" 2026-12-22 ") == DEC22
    with pytest.raises(ValueError, match="The --depart 'next week' should look like 2026-12-22"):
        parse_date("next week", "--depart")


# --- adding a trip ----------------------------------------------------------------------------------------------

def test_a_trip_is_saved_with_normalized_codes_gateways_and_defaults(conn):
    trip = row(conn, kelowna(conn))

    assert (trip["name"], trip["origin"], trip["destination"]) == ("Kelowna", "RNO", "YLW")
    assert (trip["depart_date"], trip["return_date"]) == ("2026-12-22", "2026-12-28")
    assert trip["active"] == 1
    assert db.get_gateways(conn, trip["id"]) == ["LAX", "PHX", "SEA"]


def test_a_trip_can_be_saved_inactive_and_one_way(conn):
    trip = row(conn, kelowna(conn, active=False, return_date=None))

    assert trip["active"] == 0 and trip["return_date"] is None


def test_the_new_trip_is_searched_only_when_active(conn):
    kelowna(conn, active=False)
    assert db.active_trips(conn) == []
    kelowna(conn, depart_date=date(2026, 12, 23))
    assert [t["depart_date"] for t in db.active_trips(conn)] == ["2026-12-23"]


@pytest.mark.parametrize("overrides, message", [
    (dict(name="  "), "needs a name"),
    (dict(origin="YLW"), "same airport"),
    (dict(origin="RENO"), "3-letter airport code"),
    (dict(return_date=date(2026, 12, 1)), "return date is before the departure date"),
    (dict(gateways=["SEA", "RNO"]), "origin or destination"),
    (dict(gateways=["SEATTLE"]), "3-letter airport code"),
])
def test_bad_trips_are_rejected_before_anything_is_saved(conn, overrides, message):
    with pytest.raises(ValueError, match=message):
        kelowna(conn, **overrides)

    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trip_gateways").fetchone()[0] == 0


def test_adding_the_same_route_and_dates_twice_is_refused(conn):
    first = kelowna(conn)

    with pytest.raises(ValueError, match=f"already exists \\(trip {first}\\)"):
        kelowna(conn, name="Kelowna again")


def test_the_same_route_on_other_dates_is_a_different_trip(conn):
    kelowna(conn)
    kelowna(conn, depart_date=date(2027, 2, 1), return_date=date(2027, 2, 5))

    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 2


def test_a_one_way_and_a_round_trip_on_the_same_day_are_different_trips(conn):
    kelowna(conn)
    kelowna(conn, return_date=None)

    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == 2


# --- changing a trip -----------------------------------------------------------------------------------------------

def test_update_changes_only_the_fields_given(conn):
    trip_id = kelowna(conn)

    db.update_trip(conn, trip_id, name="Kelowna in December")

    trip = row(conn, trip_id)
    assert (trip["name"], trip["depart_date"], trip["return_date"], trip["active"]) == ("Kelowna in December", "2026-12-22", "2026-12-28", 1)


def test_update_can_move_dates_deactivate_and_go_one_way(conn):
    trip_id = kelowna(conn)

    db.update_trip(conn, trip_id, depart_date=date(2026, 12, 23), active=False)
    assert (row(conn, trip_id)["depart_date"], row(conn, trip_id)["active"]) == ("2026-12-23", 0)

    db.update_trip(conn, trip_id, one_way=True)
    assert row(conn, trip_id)["return_date"] is None


def test_update_refuses_a_return_before_the_departure(conn):
    trip_id = kelowna(conn)

    with pytest.raises(ValueError, match="return date is before"):
        db.update_trip(conn, trip_id, depart_date=date(2027, 1, 5))   # the existing return is Dec 28
    assert row(conn, trip_id)["depart_date"] == "2026-12-22"


def test_update_rejects_unknown_trips(conn):
    with pytest.raises(LookupError):
        db.update_trip(conn, 999, name="x")


# --- listing ------------------------------------------------------------------------------------------------------------

def test_the_listing_shows_every_trip_active_or_not(conn):
    kelowna(conn)
    kelowna(conn, name="Old idea", depart_date=date(2027, 3, 1), return_date=None, active=False, gateways=[])

    lines = format_trips(conn).splitlines()

    assert lines[0].startswith("ID") and len(lines) == 3
    assert "Kelowna" in lines[1] and "RNO > YLW" in lines[1] and "Dec 22 2026 - Dec 28" in lines[1] and "LAX, PHX, SEA" in lines[1]
    assert "yes" in lines[1] and "no" in lines[2] and "(one way)" in lines[2] and lines[2].rstrip().endswith("none")


def test_an_empty_list_says_how_to_add_one(conn):
    assert "trip add" in format_trips(conn)


def test_a_trip_is_exact_unless_saved_flexible(conn):
    assert row(conn, kelowna(conn))["flexible"] == 0
    assert row(conn, kelowna(conn, depart_date=date(2027, 2, 1), return_date=date(2027, 2, 5), flexible=True))["flexible"] == 1


def test_the_listing_marks_flexible_trips(conn):
    kelowna(conn, flexible=True)
    kelowna(conn, name="Exact", depart_date=date(2027, 3, 1), return_date=None)

    lines = format_trips(conn).splitlines()

    assert "Flexible" in lines[0] and "-3 / ±3" in lines[1] and "exact" in lines[2]
