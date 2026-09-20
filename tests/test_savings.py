from datetime import date, datetime, timezone

import pytest

from flight_optimizer import db
from flight_optimizer.affordability import assess
from flight_optimizer.affordability import TripOption
from flight_optimizer.cash_fares import CashFare, CashSearch
from flight_optimizer.itineraries import Itinerary
from flight_optimizer.savings import TripBaseline, estimate_value, trip_baseline
from flight_optimizer.seats_aero import AwardResult, Cabin

JAN9, JAN16 = date(2027, 1, 9), date(2027, 1, 16)


def fare(price, layovers=(), total=600):
    return CashFare(price=price, total_duration=total, layovers=tuple(layovers),
                    airlines=("United",), flight_numbers=("UA 1",), cabins=("x",))


def option(miles):
    leg = AwardResult(
        source="aeroplan", origin="LAX", destination="SJO", date=JAN9, cabin=Cabin.BUSINESS,
        miles=miles, total_taxes=0, currency="USD", remaining_seats=2, airlines="AC",
        direct=True, updated_at="2026-09-19T00:00:00Z",
    )
    itinerary = Itinerary(legs=(leg,), via=None)
    return TripOption(itineraries=(itinerary,), affordability=assess([itinerary], {"aeroplan": miles}))


def test_baseline_totals_sum_directions_and_are_none_if_any_direction_is_missing():
    both = TripBaseline(cabin_fares=(fare(1500), fare(1400)), economy_fares=(fare(320), fare(300)))
    partial = TripBaseline(cabin_fares=(fare(1500), None), economy_fares=(fare(320), fare(300)))

    assert both.cabin_total == 2900 and both.economy_total == 620
    assert partial.cabin_total is None and partial.economy_total == 620


def test_estimate_value_computes_cents_per_mile_and_premium():
    baseline = TripBaseline(cabin_fares=(fare(1505),), economy_fares=(fare(323),))

    value = estimate_value(option(50_000), baseline)

    assert value.cabin_cash == 1505 and value.economy_cash == 323
    assert value.cents_per_mile_vs_cabin == pytest.approx(3.01)
    assert value.cents_per_mile_vs_economy == pytest.approx(0.646)
    assert value.upgrade_premium == 1182


def test_estimate_value_with_missing_baseline_leaves_those_fields_none():
    baseline = TripBaseline(cabin_fares=(None,), economy_fares=(fare(323),))

    value = estimate_value(option(50_000), baseline)

    assert value.cabin_cash is None and value.cents_per_mile_vs_cabin is None
    assert value.upgrade_premium is None
    assert value.cents_per_mile_vs_economy == pytest.approx(0.646)


class RecordingClient:
    """Stands in for SerpApiClient; returns canned fares keyed by (origin, dest, cabin)."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def search(self, origin, destination, on_date, cabin, **_):
        self.calls.append((origin, destination, on_date, cabin))
        return CashSearch(
            fares=tuple(self.table[(origin, destination, cabin)]),
            lowest_price=None, typical_range=None,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def add_trip(conn, **overrides):
    fields = dict(name="CR", origin="RNO", destination="SJO", depart_date=JAN9, return_date=JAN16)
    trip_id = db.add_trip(conn, **{**fields, **overrides})
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


def test_trip_baseline_applies_the_layover_rule_per_direction_and_cabin(conn):
    client = RecordingClient({
        ("RNO", "SJO", Cabin.BUSINESS): [fare(618, (661,), 1215), fare(1505, (238,), 795)],
        ("SJO", "RNO", Cabin.BUSINESS): [fare(1400, (120,), 700)],
        ("RNO", "SJO", Cabin.ECONOMY): [fare(323, (122,), 739)],
        ("SJO", "RNO", Cabin.ECONOMY): [fare(300, (90,), 650)],
    })

    baseline = trip_baseline(conn, client, add_trip(conn))

    assert baseline.cabin_total == 1505 + 1400
    assert baseline.economy_total == 323 + 300
    assert len(client.calls) == 4


def test_one_way_trip_only_searches_one_direction(conn):
    client = RecordingClient({
        ("RNO", "SJO", Cabin.BUSINESS): [fare(1505)],
        ("RNO", "SJO", Cabin.ECONOMY): [fare(323)],
    })

    baseline = trip_baseline(conn, client, add_trip(conn, return_date=None))

    assert len(baseline.cabin_fares) == 1 and baseline.cabin_total == 1505
    assert len(client.calls) == 2


def test_trip_baseline_reuses_the_cache_on_a_second_call(conn):
    client = RecordingClient({
        ("RNO", "SJO", Cabin.BUSINESS): [fare(1505)],
        ("RNO", "SJO", Cabin.ECONOMY): [fare(323)],
    })
    trip = add_trip(conn, return_date=None)

    trip_baseline(conn, client, trip)
    trip_baseline(conn, client, trip)

    assert len(client.calls) == 2
