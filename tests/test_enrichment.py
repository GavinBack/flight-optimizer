from datetime import date, datetime, timedelta

import pytest

from flight_optimizer.enrichment import (
    EnrichmentStats,
    enrich_itinerary,
    enrich_result,
)
from flight_optimizer.itineraries import Itinerary, TripSearchResult
from flight_optimizer.seats_aero import (
    AwardResult,
    Cabin,
    FlightOption,
    SeatsAeroAuthError,
    SeatsAeroError,
    SeatsAeroRateLimitError,
    TripDetails,
)

JAN9 = date(2027, 1, 9)
LAND = datetime(2027, 1, 9, 10, 0)  # first leg lands at the hub


def flight(departs, *, arrives=None, miles=25_000, seats=2, cabin=Cabin.BUSINESS, stops=0, numbers="XX1"):
    return FlightOption(
        cabin=cabin, miles=miles, seats=seats, departs_at=departs,
        arrives_at=arrives or departs + timedelta(hours=3), flight_numbers=numbers, stops=stops,
        total_duration=180, total_taxes=10_600, currency="CAD", updated_at="2026-09-19T00:00:00Z",
    )


def arriving_at_hub(miles=10_000):
    return flight(LAND - timedelta(hours=2), arrives=LAND, miles=miles, numbers="UA1")


def details(*options, url="https://example.test/book"):
    return TripDetails(options=tuple(options), booking_url=url, booking_links=(("Book", url),))


def award(origin, dest, availability_id, *, miles=1, source="aeroplan", seats=2):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=JAN9, cabin=Cabin.BUSINESS,
        miles=miles, total_taxes=0, currency="USD", remaining_seats=seats, airlines="XX",
        direct=True, updated_at="2026-05-01T00:00:00Z", availability_id=availability_id,
    )


def two_leg(first_id="A", second_id="B", *, first_source="delta", second_source="aeroplan"):
    return Itinerary(
        legs=(award("RNO", "LAX", first_id, source=first_source), award("LAX", "SJO", second_id, source=second_source)),
        via="LAX",
    )


class FakeClient:
    def __init__(self, table, errors=None):
        self.table = table
        self.errors = errors or {}
        self.calls = []

    def trips(self, availability_id, version=None):
        self.versions = getattr(self, 'versions', []) + [version]
        self.calls.append(availability_id)
        if availability_id in self.errors:
            raise self.errors[availability_id]
        return self.table[availability_id]


def run(client, itinerary, **kwargs):
    stats = EnrichmentStats()
    result = enrich_itinerary(client, itinerary, cabin=Cabin.BUSINESS, cache={}, stats=stats, **kwargs)
    return result, stats


def test_picks_the_comfortable_pairing_and_uses_that_flights_price():
    client = FakeClient({
        "A": details(arriving_at_hub(miles=10_900)),
        "B": details(
            flight(LAND + timedelta(minutes=90), miles=25_000, numbers="AV600"),   # tight, cheaper
            flight(LAND + timedelta(minutes=240), miles=30_000, numbers="AV641"),  # comfortable
        ),
    })

    enriched, _ = run(client, two_leg())

    first, second = enriched.legs
    assert second.flight_numbers == "AV641"
    assert second.miles == 30_000                      # not the cheaper row price
    assert enriched.total_miles == 10_900 + 30_000
    assert enriched.connection_minutes == 240
    assert second.booking_url == "https://example.test/book"
    assert first.arrives_at == LAND and second.departs_at == LAND + timedelta(minutes=240)
    assert first.remaining_seats == 2 and first.direct is True


def test_connection_of_an_hour_or_less_is_dropped_and_counted():
    client = FakeClient({
        "A": details(arriving_at_hub()),
        "B": details(flight(LAND + timedelta(minutes=60)), flight(LAND + timedelta(minutes=20))),
    })

    enriched, stats = run(client, two_leg())

    assert enriched is None
    assert stats.dropped_unsafe == 1 and stats.unverifiable == 0


def test_tight_connection_is_kept_for_the_report_to_warn_about():
    client = FakeClient({"A": details(arriving_at_hub()), "B": details(flight(LAND + timedelta(minutes=90)))})

    enriched, _ = run(client, two_leg())

    assert enriched.connection_minutes == 90


@pytest.mark.parametrize("error", [SeatsAeroError("HTTP 404", 404)])
def test_failed_lookup_means_unverifiable_not_recommended(error):
    client = FakeClient({"A": details(arriving_at_hub())}, errors={"B": error})

    enriched, stats = run(client, two_leg())

    assert enriched is None and stats.unverifiable == 1


def test_missing_availability_id_is_unverifiable():
    itinerary = Itinerary(legs=(award("RNO", "LAX", ""), award("LAX", "SJO", "B")), via="LAX")

    enriched, stats = run(FakeClient({}), itinerary)

    assert enriched is None and stats.unverifiable == 1


def test_no_bookable_flights_in_the_cabin_is_unverifiable():
    client = FakeClient({
        "A": details(arriving_at_hub()),
        "B": details(flight(LAND + timedelta(hours=4), seats=0), flight(LAND + timedelta(hours=5), cabin=Cabin.ECONOMY)),
    })

    enriched, stats = run(client, two_leg())

    assert enriched is None and stats.unverifiable == 1


def test_min_seats_applies_to_the_flights_that_are_picked():
    client = FakeClient({"A": details(arriving_at_hub()), "B": details(flight(LAND + timedelta(hours=4), seats=2))})

    assert run(client, two_leg(), min_seats=2)[0] is not None
    assert run(client, two_leg(), min_seats=3)[0] is None


@pytest.mark.parametrize("error", [SeatsAeroAuthError("bad key", 401), SeatsAeroRateLimitError("quota", 429)])
def test_auth_and_quota_errors_stop_the_whole_run(error):
    client = FakeClient({"A": details(arriving_at_hub())}, errors={"B": error})

    with pytest.raises(type(error)):
        run(client, two_leg())


def test_shared_legs_are_looked_up_once():
    client = FakeClient({
        "A": details(arriving_at_hub()),
        "B": details(flight(LAND + timedelta(hours=4))),
        "C": details(flight(LAND + timedelta(hours=5))),
    })
    stats, cache = EnrichmentStats(), {}

    for itinerary in (two_leg("A", "B"), two_leg("A", "C")):
        enrich_itinerary(client, itinerary, cabin=Cabin.BUSINESS, cache=cache, stats=stats)

    assert client.calls == ["A", "B", "C"] and stats.lookups == 3


def test_single_leg_itinerary_gets_its_cheapest_flight_and_link():
    client = FakeClient({"S": details(flight(LAND, miles=60_000, numbers="X2"), flight(LAND, miles=50_000, numbers="X1"))})
    itinerary = Itinerary(legs=(award("RNO", "SJO", "S"),), via=None)

    enriched, _ = run(client, itinerary)

    assert enriched.legs[0].flight_numbers == "X1" and enriched.total_miles == 50_000
    assert enriched.legs[0].booking_url


# ---- enrich_result -----------------------------------------------------------

def result_with(outbound, return_trip=None):
    return TripSearchResult(trip_id=1, outbound=outbound, return_trip=return_trip)


def good_client(*ids):
    table = {}
    for i in ids:
        table[i] = details(arriving_at_hub(), flight(LAND + timedelta(hours=4)))
    return FakeClient(table)


def test_enrich_result_keeps_at_most_per_direction_verified_itineraries():
    client = good_client("A", "B", "C", "D", "E", "F")
    outbound = [two_leg("A", "B"), two_leg("C", "D"), two_leg("E", "F")]

    enriched, stats = enrich_result(client, result_with(outbound), cabin=Cabin.BUSINESS,
                                    balances={"delta": 100_000, "capital_one": 100_000}, per_direction=2)

    assert len(enriched.outbound) == 2 and enriched.return_trip is None
    assert stats.lookups == 4  # stopped before looking up the third


def test_enrich_result_skips_over_failures_to_find_verified_ones():
    unsafe = {"A": details(arriving_at_hub()), "B": details(flight(LAND + timedelta(minutes=30)))}
    client = FakeClient({**unsafe, **good_client("C", "D").table})
    outbound = [two_leg("A", "B"), two_leg("C", "D")]

    enriched, stats = enrich_result(client, result_with(outbound), cabin=Cabin.BUSINESS,
                                    balances={"delta": 100_000, "capital_one": 100_000}, per_direction=1)

    assert [i.legs[0].availability_id for i in enriched.outbound] == ["C"]
    assert stats.dropped_unsafe == 1


def test_enrich_result_stops_at_the_lookup_budget_and_says_so():
    client = FakeClient({}, errors={"A": SeatsAeroError("x", 404), "C": SeatsAeroError("x", 404)})
    outbound = [two_leg("A", "B"), two_leg("C", "D"), two_leg("E", "F")]

    enriched, stats = enrich_result(client, result_with(outbound), cabin=Cabin.BUSINESS,
                                    balances={}, max_lookups_per_direction=2)

    assert enriched.outbound == [] and stats.budget_reached and stats.unverifiable == 2
    assert stats.lookups == 2


def test_enrich_result_looks_up_affordable_candidates_before_unaffordable_ones():
    client = good_client("A", "B", "C", "D")
    cheap_but_unaffordable = two_leg("A", "B", first_source="united", second_source="united")
    affordable = two_leg("C", "D")  # delta + aeroplan, funded below

    enriched, _ = enrich_result(client, result_with([cheap_but_unaffordable, affordable]), cabin=Cabin.BUSINESS,
                                balances={"delta": 100_000, "capital_one": 100_000}, per_direction=1)

    assert enriched.outbound[0].legs[0].availability_id == "C"


def test_enrich_result_handles_both_directions():
    client = good_client("A", "B", "C", "D")

    enriched, _ = enrich_result(client, result_with([two_leg("A", "B")], [two_leg("C", "D")]), cabin=Cabin.BUSINESS,
                                balances={"delta": 100_000, "capital_one": 100_000})

    assert len(enriched.outbound) == 1 and len(enriched.return_trip) == 1


def test_the_chosen_flights_stop_count_is_recorded_on_the_leg():
    client = FakeClient({
        "A": details(arriving_at_hub()),
        "B": details(flight(LAND + timedelta(hours=4), stops=2, numbers="AA1, AA2, AA3")),
    })

    enriched, _ = run(client, two_leg())

    assert enriched.legs[0].stops == 0 and enriched.legs[1].stops == 2
    assert enriched.hub_leg_stops == 2 and enriched.legs[1].direct is False


# --- the outbound must land on the days asked -------------------------------------------------

JAN8 = date(2027, 1, 8)


def one_leg(availability_id="D"):
    return Itinerary(legs=(award("RNO", "SJO", availability_id),), via=None)


def test_a_flight_leaving_the_day_before_and_landing_on_the_day_is_kept():
    red_eye = flight(datetime(2027, 1, 8, 23, 0), arrives=datetime(2027, 1, 9, 6, 0), miles=30_000, numbers="RED1")
    daytime = flight(datetime(2027, 1, 8, 9, 0), arrives=datetime(2027, 1, 8, 15, 0), miles=20_000, numbers="DAY1")
    client = FakeClient({"D": details(daytime, red_eye)})

    result, stats = run(client, one_leg(), lands=(JAN9, JAN9))

    # the cheaper flight lands a day early, so the red-eye is chosen instead of dropping the row
    assert result.legs[0].flight_numbers == "RED1" and result.legs[0].miles == 30_000
    assert stats.wrong_landing_day == 0


def test_an_outbound_landing_a_day_early_or_late_is_dropped_and_counted():
    early = flight(datetime(2027, 1, 8, 9, 0), arrives=datetime(2027, 1, 8, 15, 0))
    late = flight(datetime(2027, 1, 9, 22, 0), arrives=datetime(2027, 1, 10, 5, 0))
    client = FakeClient({"E": details(early), "L": details(late)})

    for availability_id in ("E", "L"):
        result, stats = run(client, one_leg(availability_id), lands=(JAN9, JAN9))
        assert result is None and stats.wrong_landing_day == 1


def test_the_landing_day_is_checked_on_the_second_leg_of_a_hub_itinerary():
    client = FakeClient({
        "A": details(arriving_at_hub()),
        "B": details(flight(LAND + timedelta(hours=4), arrives=datetime(2027, 1, 10, 1, 0))),  # lands after midnight
    })

    result, stats = run(client, two_leg(), lands=(JAN9, JAN9))

    assert result is None and stats.wrong_landing_day == 1


def test_enrich_result_applies_the_landing_days_to_the_outbound_only():
    late = flight(datetime(2027, 1, 9, 22, 0), arrives=datetime(2027, 1, 10, 5, 0))
    client = FakeClient({"O": details(late), "R": details(late)})
    result = TripSearchResult(
        trip_id=1, outbound=[one_leg("O")], return_trip=[one_leg("R")], outbound_lands=(JAN9, JAN9),
    )

    verified, stats = enrich_result(client, result, cabin=Cabin.BUSINESS, balances={"aeroplan": 100_000})

    assert verified.outbound == [] and len(verified.return_trip) == 1
    assert verified.outbound_lands == (JAN9, JAN9) and stats.wrong_landing_day == 1


def test_an_outbound_landing_two_days_after_it_leaves_is_dropped_even_inside_the_window():
    # flexible window: land Dec 19-22. Leaving Dec 18 and landing Dec 20 is inside it, but two days on the way.
    dec18 = date(2026, 12, 18)
    first = AwardResult(
        source="aeroplan", origin="RNO", destination="LAX", date=dec18, cabin=Cabin.BUSINESS, miles=1,
        total_taxes=0, currency="USD", remaining_seats=2, airlines="XX", direct=True,
        updated_at="2026-05-01T00:00:00Z", availability_id="A",
    )
    second = AwardResult(
        source="aeroplan", origin="LAX", destination="YLW", date=date(2026, 12, 19), cabin=Cabin.BUSINESS, miles=1,
        total_taxes=0, currency="USD", remaining_seats=2, airlines="XX", direct=True,
        updated_at="2026-05-01T00:00:00Z", availability_id="B",
    )
    client = FakeClient({
        "A": details(flight(datetime(2026, 12, 18, 15, 0), arrives=datetime(2026, 12, 18, 17, 0))),
        "B": details(flight(datetime(2026, 12, 19, 17, 0), arrives=datetime(2026, 12, 20, 0, 15))),
    })

    result, stats = run(client, Itinerary(legs=(first, second), via="LAX"),
                        lands=(date(2026, 12, 19), date(2026, 12, 22)))

    assert result is None and stats.wrong_landing_day == 1
