from datetime import date

import pytest

from flight_optimizer import db
from flight_optimizer.itineraries import (
    combine_via_gateway,
    search_active_trips,
    search_one_way,
    search_trip,
)
from flight_optimizer.seats_aero import AwardResult, Cabin


def award(origin, destination, on, miles, *, source="united", cabin=Cabin.BUSINESS, seats=2):
    return AwardResult(
        source=source, origin=origin, destination=destination, date=on, cabin=cabin,
        miles=miles, total_taxes=0, currency="USD", remaining_seats=seats,
        airlines="UA", direct=True, updated_at="2026-09-19T00:00:00Z",
    )


class FakeClient:
    """Answers `search` from a {(origin, destination): [results]} table and logs calls."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def search(self, origin, destination, start, end, *, sources=None, **_):
        self.calls.append((origin, destination, start, end))
        return [r for r in self.table.get((origin, destination), []) if start <= r.date <= end]


JAN9, JAN10, JAN11, JAN16 = (date(2027, 1, d) for d in (9, 10, 11, 16))


def test_combine_pairs_same_day_and_next_day_but_not_earlier_or_later():
    first = [award("RNO", "LAX", JAN10, 10_000)]
    second = [
        award("LAX", "SJO", JAN9, 1),   # before the first leg
        award("LAX", "SJO", JAN10, 2),  # same day
        award("LAX", "SJO", JAN11, 3),  # next day
        award("LAX", "SJO", date(2027, 1, 12), 4),  # too long a layover
    ]

    result = combine_via_gateway(first, second, "LAX", max_layover_days=1)

    assert [i.legs[1].miles for i in result] == [2, 3]
    assert all(i.via == "LAX" for i in result)


def test_itinerary_totals_miles_overall_and_per_program():
    (itinerary,) = combine_via_gateway(
        [award("RNO", "LAX", JAN9, 10_900, source="delta")],
        [award("LAX", "SJO", JAN9, 25_000, source="aeroplan")],
        "LAX",
    )

    assert itinerary.total_miles == 35_900
    assert itinerary.miles_by_program == {"delta": 10_900, "aeroplan": 25_000}


def test_search_one_way_combines_gateways_and_sorts_cheapest_first():
    client = FakeClient({
        ("RNO", "LAX"): [award("RNO", "LAX", JAN9, 10_900, source="delta")],
        ("LAX", "SJO"): [award("LAX", "SJO", JAN9, 25_000, source="aeroplan")],
        ("RNO", "DEN"): [award("RNO", "DEN", JAN9, 35_600)],
        ("DEN", "SJO"): [award("DEN", "SJO", JAN9, 20_000)],
    })

    result = search_one_way(
        client, "RNO", "SJO", JAN9, gateways=["DEN", "LAX"], cabin=Cabin.BUSINESS
    )

    assert [(i.via, i.total_miles) for i in result] == [("LAX", 35_900), ("DEN", 55_600)]


def test_search_one_way_includes_a_direct_pair_when_one_exists():
    client = FakeClient({("RNO", "SJO"): [award("RNO", "SJO", JAN9, 50_000)]})

    (only,) = search_one_way(
        client, "RNO", "SJO", JAN9, gateways=["LAX"], cabin=Cabin.BUSINESS
    )

    assert only.via is None and len(only.legs) == 1


def test_search_one_way_filters_to_the_requested_cabin():
    client = FakeClient({
        ("RNO", "SJO"): [
            award("RNO", "SJO", JAN9, 30_000, cabin=Cabin.ECONOMY),
            award("RNO", "SJO", JAN9, 60_000, cabin=Cabin.BUSINESS),
        ],
    })

    result = search_one_way(client, "RNO", "SJO", JAN9, gateways=[], cabin=Cabin.BUSINESS)

    assert [i.total_miles for i in result] == [60_000]


def test_second_leg_is_not_searched_when_first_leg_is_empty():
    client = FakeClient({("LAX", "SJO"): [award("LAX", "SJO", JAN9, 25_000)]})

    result = search_one_way(
        client, "RNO", "SJO", JAN9, gateways=["LAX"], cabin=Cabin.BUSINESS
    )

    assert result == []
    assert ("LAX", "SJO", JAN9, JAN10) not in client.calls


def test_gateway_equal_to_origin_or_destination_is_skipped():
    client = FakeClient({})

    search_one_way(client, "RNO", "SJO", JAN9, gateways=["RNO", "SJO"], cabin=Cabin.BUSINESS)

    assert client.calls == [("RNO", "SJO", JAN9, JAN9)]  # just the direct pair


def test_zero_seat_legs_are_dropped_by_default():
    client = FakeClient({
        ("RNO", "SJO"): [award("RNO", "SJO", JAN9, 50_000, seats=0)],
        ("RNO", "LAX"): [award("RNO", "LAX", JAN9, 10_000, seats=0), award("RNO", "LAX", JAN9, 12_000, seats=1)],
        ("LAX", "SJO"): [award("LAX", "SJO", JAN9, 25_000, seats=3)],
    })

    result = search_one_way(client, "RNO", "SJO", JAN9, gateways=["LAX"], cabin=Cabin.BUSINESS)

    assert [i.total_miles for i in result] == [37_000]  # only the 12,000 first leg survives


def test_zero_seat_second_leg_removes_the_itinerary():
    client = FakeClient({
        ("RNO", "LAX"): [award("RNO", "LAX", JAN9, 10_000)],
        ("LAX", "SJO"): [award("LAX", "SJO", JAN9, 25_000, seats=0)],
    })

    assert search_one_way(client, "RNO", "SJO", JAN9, gateways=["LAX"], cabin=Cabin.BUSINESS) == []


def test_min_seats_is_adjustable():
    table = {
        ("RNO", "SJO"): [award("RNO", "SJO", JAN9, 50_000, seats=0), award("RNO", "SJO", JAN9, 60_000, seats=2)],
    }
    kwargs = dict(gateways=[], cabin=Cabin.BUSINESS)

    keep_all = search_one_way(FakeClient(table), "RNO", "SJO", JAN9, min_seats=0, **kwargs)
    two_travelers = search_one_way(FakeClient(table), "RNO", "SJO", JAN9, min_seats=3, **kwargs)

    assert [i.total_miles for i in keep_all] == [50_000, 60_000]
    assert two_travelers == []


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def add_trip(conn, **overrides):
    fields = dict(
        name="Costa Rica", origin="RNO", destination="SJO",
        depart_date=JAN9, return_date=JAN16, active=True,
    )
    return db.add_trip(conn, **{**fields, **overrides})


def test_search_trip_searches_outbound_and_return_legs(conn):
    trip_id = add_trip(conn)
    client = FakeClient({
        ("RNO", "LAX"): [award("RNO", "LAX", JAN9, 10_000)],
        ("LAX", "SJO"): [award("LAX", "SJO", JAN9, 25_000)],
        ("SJO", "LAX"): [award("SJO", "LAX", JAN16, 26_000)],
        ("LAX", "RNO"): [award("LAX", "RNO", JAN16, 11_000)],
    })
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

    result = search_trip(client, trip, ["LAX"])

    assert [i.total_miles for i in result.outbound] == [35_000]
    assert [i.total_miles for i in result.return_trip] == [37_000]


def test_one_way_trip_has_no_return_search(conn):
    trip_id = add_trip(conn, return_date=None)
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

    result = search_trip(FakeClient({}), trip, [])

    assert result.return_trip is None


def test_search_active_trips_skips_inactive_trips(conn):
    active_id = add_trip(conn, name="Active")
    inactive_id = add_trip(conn, name="Shelved", active=False)
    db.set_gateways(conn, active_id, ["LAX"])
    client = FakeClient({})

    results = search_active_trips(conn, client)

    assert list(results) == [active_id]
    assert inactive_id not in results
    # The inactive trip's searches never happened.
    assert client.calls and all(call[2] in (JAN9, JAN16) for call in client.calls)
