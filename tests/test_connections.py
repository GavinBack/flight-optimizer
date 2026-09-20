from datetime import date, datetime, timedelta

import pytest

from flight_optimizer.connections import (
    COMFORTABLE_CONNECTION_MINUTES,
    MIN_CONNECTION_MINUTES,
    ConnectionClass,
    best_pairing,
    classify,
    connection_minutes,
)
from flight_optimizer.itineraries import Itinerary
from flight_optimizer.seats_aero import AwardResult, Cabin, FlightOption

ARRIVE = datetime(2027, 1, 9, 10, 0)


def depart_after(minutes):
    return ARRIVE + timedelta(minutes=minutes)


def option(departs, arrives=None, miles=25_000):
    departs = departs
    return FlightOption(
        cabin=Cabin.BUSINESS, miles=miles, seats=2, departs_at=departs,
        arrives_at=arrives or departs + timedelta(hours=3), flight_numbers="XX1", stops=0,
        total_duration=180, total_taxes=0, currency="USD", updated_at="2026-09-19T00:00:00Z",
    )


def arriving(at):
    return option(at - timedelta(hours=2), arrives=at)


def test_the_owners_thresholds():
    assert MIN_CONNECTION_MINUTES == 60
    assert COMFORTABLE_CONNECTION_MINUTES == 150


@pytest.mark.parametrize("minutes, expected", [
    (-30, ConnectionClass.UNSAFE),        # departs before it lands
    (0, ConnectionClass.UNSAFE),
    (45, ConnectionClass.UNSAFE),
    (60, ConnectionClass.UNSAFE),         # exactly 1 hour: not recommended
    (61, ConnectionClass.TIGHT),          # just over 1 hour: warn
    (90, ConnectionClass.TIGHT),
    (149, ConnectionClass.TIGHT),         # just under 2.5 hours: warn
    (150, ConnectionClass.COMFORTABLE),   # exactly 2.5 hours: fine
    (300, ConnectionClass.COMFORTABLE),
])
def test_same_day_classification_boundaries(minutes, expected):
    assert classify(ARRIVE, depart_after(minutes)) is expected


def test_crossing_midnight_is_overnight_however_long():
    late_arrival = datetime(2027, 1, 9, 21, 0)

    assert classify(late_arrival, datetime(2027, 1, 10, 0, 30)) is ConnectionClass.OVERNIGHT
    assert classify(late_arrival, datetime(2027, 1, 10, 9, 0)) is ConnectionClass.OVERNIGHT


def test_a_short_wait_that_crosses_midnight_is_still_unsafe():
    assert classify(datetime(2027, 1, 9, 23, 30), datetime(2027, 1, 10, 0, 10)) is ConnectionClass.UNSAFE


def test_ranking_penalty_order_matches_the_owners_preference():
    assert (ConnectionClass.COMFORTABLE < ConnectionClass.TIGHT
            < ConnectionClass.OVERNIGHT < ConnectionClass.UNSAFE)


def test_connection_minutes_is_signed_whole_minutes():
    assert connection_minutes(ARRIVE, depart_after(95)) == 95
    assert connection_minutes(ARRIVE, depart_after(-10)) == -10


def test_best_pairing_prefers_a_comfortable_connection_even_if_it_costs_more():
    inbound = [arriving(ARRIVE)]
    cheap_but_tight = option(depart_after(90), miles=20_000)
    dearer_but_comfortable = option(depart_after(200), miles=30_000)

    pairing = best_pairing(inbound, [cheap_but_tight, dearer_but_comfortable])

    assert pairing.connection is ConnectionClass.COMFORTABLE
    assert pairing.second.miles == 30_000
    assert pairing.minutes == 200


def test_best_pairing_never_returns_an_unsafe_connection():
    inbound = [arriving(ARRIVE)]

    assert best_pairing(inbound, [option(depart_after(60)), option(depart_after(30))]) is None


def test_best_pairing_falls_back_to_tight_then_overnight():
    inbound = [arriving(ARRIVE)]
    tight, overnight = option(depart_after(90)), option(depart_after(24 * 60))

    assert best_pairing(inbound, [overnight, tight]).connection is ConnectionClass.TIGHT
    assert best_pairing(inbound, [overnight]).connection is ConnectionClass.OVERNIGHT


def test_best_pairing_breaks_ties_on_miles_then_longer_buffer():
    inbound = [arriving(ARRIVE)]
    a = option(depart_after(160), miles=25_000)
    b = option(depart_after(240), miles=25_000)
    c = option(depart_after(170), miles=26_000)

    assert best_pairing(inbound, [a, b, c]).minutes == 240


def test_best_pairing_uses_the_inbound_flight_that_makes_it_work():
    late, early = arriving(ARRIVE + timedelta(hours=2)), arriving(ARRIVE - timedelta(hours=3))

    pairing = best_pairing([late, early], [option(depart_after(30))])

    assert pairing.first is early


def test_best_pairing_with_no_options_is_none():
    assert best_pairing([], [option(depart_after(200))]) is None


def leg(origin, dest, on, *, arrives=None, departs=None):
    return AwardResult(
        source="aeroplan", origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS,
        miles=1, total_taxes=0, currency="USD", remaining_seats=2, airlines="XX", direct=True,
        updated_at="", departs_at=departs, arrives_at=arrives,
    )


def test_itinerary_reports_connection_class_and_minutes_when_times_are_known():
    it = Itinerary(
        legs=(leg("RNO", "LAX", date(2027, 1, 9), arrives=ARRIVE),
              leg("LAX", "SJO", date(2027, 1, 9), departs=depart_after(95))),
        via="LAX",
    )

    assert it.connection_minutes == 95
    assert it.connection_class is ConnectionClass.TIGHT


def test_itinerary_without_times_or_without_a_connection_has_no_class():
    unknown = Itinerary(legs=(leg("RNO", "LAX", date(2027, 1, 9)), leg("LAX", "SJO", date(2027, 1, 9))), via="LAX")
    single = Itinerary(legs=(leg("RNO", "SJO", date(2027, 1, 9), arrives=ARRIVE),), via=None)

    assert unknown.connection_class is None and unknown.connection_minutes is None
    assert single.connection_class is None
