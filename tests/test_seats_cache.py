from datetime import date, datetime, timedelta, timezone

import pytest

from flight_optimizer import db
from flight_optimizer.enrichment import EnrichmentStats, enrich_itinerary, enrich_result
from flight_optimizer.itineraries import Itinerary, TripSearchResult
from flight_optimizer.seats_aero import (
    AwardResult, Cabin, FlightOption, SeatsAeroError, TripDetails,
)
from flight_optimizer.seats_cache import (
    CachedSeatsAero, award_from_dict, award_to_dict, details_from_dict, details_to_dict,
)

JAN9 = date(2027, 1, 9)
T0 = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)


def award(availability_id="A1", *, updated="2026-09-18T01:33:05.143957Z", miles=25_000, origin="LAX", dest="SJO"):
    return AwardResult(
        source="aeroplan", origin=origin, destination=dest, date=JAN9, cabin=Cabin.BUSINESS, miles=miles,
        total_taxes=10_600, currency="CAD", remaining_seats=5, airlines="AV", direct=True,
        updated_at=updated, availability_id=availability_id,
    )


def details(miles=25_000, url="https://book.example/x"):
    option = FlightOption(
        cabin=Cabin.BUSINESS, miles=miles, seats=5, departs_at=datetime(2027, 1, 9, 13, 40),
        arrives_at=datetime(2027, 1, 9, 23, 15), flight_numbers="AV641", stops=0, total_duration=455,
        total_taxes=10_600, currency="CAD", updated_at="2026-09-18T01:33:05.143957Z",
    )
    return TripDetails(options=(option,), booking_url=url, booking_links=(("Book via Aeroplan", url),))


class FakeRaw:
    """Stands in for SeatsAeroClient: counts real calls, can be told to fail."""

    def __init__(self, search_results=None, trip_details=None):
        self.search_results = search_results if search_results is not None else [award()]
        self.trip_details = trip_details or details()
        self.calls_made = 0
        self.calls_remaining = 900
        self.fail = False

    def search(self, origin, destination, start, end, *, sources=None, take=100, max_pages=10):
        self.calls_made += 1
        if self.fail:
            raise SeatsAeroError("boom", 500)
        return list(self.search_results)

    def trips(self, availability_id, version=None):
        self.calls_made += 1
        if self.fail:
            raise SeatsAeroError("boom", 500)
        return self.trip_details


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def make(conn, raw=None, clock=None, **kwargs):
    raw = raw or FakeRaw()
    clock = clock or Clock()
    return CachedSeatsAero(raw, conn, clock=clock, **kwargs), raw, clock


def search(cache, **kwargs):
    return cache.search("LAX", "SJO", JAN9, JAN9, **kwargs)


# --- serialization ---------------------------------------------------------------

def test_award_result_survives_a_round_trip_with_and_without_times():
    plain = award()
    timed = AwardResult(**{**plain.__dict__, "departs_at": datetime(2027, 1, 9, 13, 40),
                            "arrives_at": datetime(2027, 1, 9, 23, 15), "flight_numbers": "AV641",
                            "booking_url": "https://x.test", "stops": 2})

    assert award_from_dict(award_to_dict(plain)) == plain
    assert award_from_dict(award_to_dict(timed)) == timed


def test_trip_details_survive_a_round_trip():
    original = details()

    assert details_from_dict(details_to_dict(original)) == original


# --- flight details: keyed by the row's version -------------------------------------

def test_flight_details_are_fetched_once_then_reused(conn):
    cache, raw, _ = make(conn)

    first = cache.trips("A1", version="v1")
    second = cache.trips("A1", version="v1")

    assert raw.calls_made == 1
    assert not first.from_cache and second.from_cache
    assert replace_flag(second) == first
    assert cache.trips_cache_hits == 1


def replace_flag(d):
    from dataclasses import replace
    return replace(d, from_cache=False)


def test_a_new_version_of_the_row_is_fetched_again(conn):
    cache, raw, _ = make(conn)
    cache.trips("A1", version="2026-09-18")

    refreshed = cache.trips("A1", version="2026-09-19")

    assert raw.calls_made == 2 and not refreshed.from_cache
    assert cache.trips("A1", version="2026-09-18").from_cache  # the old version is still there


def test_without_a_version_nothing_is_cached(conn):
    cache, raw, _ = make(conn)

    cache.trips("A1")
    cache.trips("A1", version="")
    cache.trips("A1", version=None)

    assert raw.calls_made == 3
    assert conn.execute("SELECT COUNT(*) FROM trips_cache").fetchone()[0] == 0


def test_a_failed_lookup_is_not_cached(conn):
    cache, raw, _ = make(conn)
    raw.fail = True

    with pytest.raises(SeatsAeroError):
        cache.trips("A1", version="v1")
    raw.fail = False

    assert not cache.trips("A1", version="v1").from_cache
    assert raw.calls_made == 2


def test_flight_details_do_not_expire_while_the_version_is_unchanged(conn):
    cache, raw, clock = make(conn)
    cache.trips("A1", version="v1")

    clock.now = T0 + timedelta(days=20)

    assert cache.trips("A1", version="v1").from_cache and raw.calls_made == 1


# --- searches: short timer ----------------------------------------------------------

def test_a_repeat_search_within_the_hour_is_served_from_cache(conn):
    cache, raw, clock = make(conn)
    first = search(cache)

    clock.now = T0 + timedelta(minutes=59)
    second = search(cache)

    assert raw.calls_made == 1 and second == first and cache.search_cache_hits == 1


def test_a_search_older_than_the_ttl_is_run_again(conn):
    cache, raw, clock = make(conn)
    search(cache)

    clock.now = T0 + timedelta(hours=1, minutes=1)
    search(cache)

    assert raw.calls_made == 2 and cache.search_cache_hits == 0


def test_the_daily_run_never_reuses_yesterdays_search(conn):
    cache, raw, clock = make(conn)
    search(cache)

    clock.now = T0 + timedelta(days=1)  # 7:00 the next morning
    search(cache)

    assert raw.calls_made == 2


def test_different_searches_do_not_share_a_cache_entry(conn):
    cache, raw, _ = make(conn)

    search(cache)
    cache.search("LAX", "SFO", JAN9, JAN9)
    cache.search("LAX", "SJO", JAN9, date(2027, 1, 10))
    search(cache, sources=["aeroplan"])

    assert raw.calls_made == 4


def test_the_order_of_sources_does_not_matter(conn):
    cache, raw, _ = make(conn)

    search(cache, sources=["united", "aeroplan"])
    search(cache, sources=["aeroplan", "united"])

    assert raw.calls_made == 1


def test_fresh_mode_always_searches_but_still_stores_the_result(conn):
    fresh, raw, clock = make(conn, use_search_cache=False)
    search(fresh)
    search(fresh)
    assert raw.calls_made == 2 and fresh.search_cache_hits == 0

    normal, _, _ = make(conn, raw=raw, clock=clock)
    search(normal)
    assert raw.calls_made == 2 and normal.search_cache_hits == 1  # fed by the fresh run


def test_an_empty_search_result_is_cached_too(conn):
    cache, raw, _ = make(conn, raw=FakeRaw(search_results=[]))

    assert search(cache) == [] and search(cache) == []
    assert raw.calls_made == 1


def test_a_failed_search_is_not_cached(conn):
    cache, raw, _ = make(conn)
    raw.fail = True

    with pytest.raises(SeatsAeroError):
        search(cache)
    raw.fail = False

    assert search(cache) == [award()] and raw.calls_made == 2


# --- bookkeeping ---------------------------------------------------------------------

def test_counters_and_passthroughs(conn):
    cache, raw, _ = make(conn)

    search(cache); search(cache); cache.trips("A1", "v1"); cache.trips("A1", "v1")

    assert (cache.api_calls, cache.cache_hits) == (2, 2)
    assert cache.calls_remaining == 900


def test_old_cache_rows_are_pruned_when_the_cache_is_created(conn):
    old_fetch = (T0 - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (T0 - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.save_trips_cache(conn, "OLD", "v", "{}", old_fetch)
    db.save_trips_cache(conn, "NEW", "v", "{}", recent)
    db.save_search_cache(conn, "old", "[]", (T0 - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.save_search_cache(conn, "new", "[]", (T0 - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    make(conn)

    assert [r["availability_id"] for r in conn.execute("SELECT * FROM trips_cache")] == ["NEW"]
    assert [r["cache_key"] for r in conn.execute("SELECT * FROM search_cache")] == ["new"]


# --- effect on enrichment ---------------------------------------------------------------

def two_leg(first_id="A", second_id="B", *, updated="2026-09-18T00:00:00Z"):
    return Itinerary(
        legs=(award(first_id, updated=updated, origin="RNO", dest="LAX"),
              award(second_id, updated=updated, origin="LAX", dest="SJO")),
        via="LAX",
    )


def hub_details():
    """Lands at LAX at 10:00, onward flight at 14:00."""
    inbound = FlightOption(cabin=Cabin.BUSINESS, miles=10_000, seats=3, departs_at=datetime(2027, 1, 9, 7),
                           arrives_at=datetime(2027, 1, 9, 10), flight_numbers="UA1", stops=0,
                           total_duration=180, total_taxes=0, currency="USD", updated_at="")
    outbound = FlightOption(cabin=Cabin.BUSINESS, miles=25_000, seats=3, departs_at=datetime(2027, 1, 9, 14),
                            arrives_at=datetime(2027, 1, 9, 20), flight_numbers="AV1", stops=0,
                            total_duration=360, total_taxes=0, currency="USD", updated_at="")
    return inbound, outbound


class HubRaw(FakeRaw):
    def trips(self, availability_id, version=None):
        self.calls_made += 1
        if self.fail:
            raise SeatsAeroError("boom", 500)
        inbound, outbound = hub_details()
        return TripDetails(options=(inbound if availability_id.startswith("A") else outbound,),
                           booking_url="", booking_links=())


def run_enrichment(cache, itineraries, **kwargs):
    result = TripSearchResult(trip_id=1, outbound=itineraries, return_trip=None)
    return enrich_result(cache, result, cabin=Cabin.BUSINESS, balances={"capital_one": 10**6}, **kwargs)


def test_a_second_run_reuses_every_flight_lookup(conn):
    cache, raw, _ = make(conn, raw=HubRaw())
    itineraries = [two_leg("A1", "B1"), two_leg("A2", "B2")]

    _, cold = run_enrichment(cache, itineraries)
    _, warm = run_enrichment(cache, itineraries)

    assert (cold.lookups, cold.cache_hits) == (4, 0)
    assert (warm.lookups, warm.cache_hits) == (0, 4)
    assert raw.calls_made == 4


def test_only_rows_that_changed_are_looked_up_again(conn):
    cache, raw, _ = make(conn, raw=HubRaw())
    run_enrichment(cache, [two_leg("A1", "B1"), two_leg("A2", "B2")])

    _, stats = run_enrichment(cache, [two_leg("A1", "B1"), two_leg("A2", "B2", updated="2026-09-20T05:00:00Z")])

    assert (stats.lookups, stats.cache_hits) == (2, 2)  # the refreshed itinerary's two legs


def test_cache_hits_do_not_spend_the_lookup_budget(conn):
    cache, raw, _ = make(conn, raw=HubRaw())
    itineraries = [two_leg(f"A{i}", f"B{i}") for i in range(5)]
    run_enrichment(cache, itineraries)  # warm the cache

    enriched, stats = run_enrichment(cache, itineraries, max_lookups_per_direction=1)

    assert len(enriched.outbound) == 5 and not stats.budget_reached and stats.lookups == 0


def test_the_leg_version_is_what_gets_passed_to_the_client(conn):
    seen = []

    class Recorder(HubRaw):
        def trips(self, availability_id, version=None):
            seen.append((availability_id, version))
            return super().trips(availability_id, version)

    cache, _, _ = make(conn, raw=Recorder())

    run_enrichment(cache, [two_leg("A1", "B1", updated="2026-09-18T01:33:05.143957Z")])

    assert seen == [("A1", None), ("B1", None)]  # the wrapper asks the raw client without a version
    assert conn.execute("SELECT DISTINCT version FROM trips_cache").fetchall()[0]["version"] == "2026-09-18T01:33:05.143957Z"


def test_a_failed_lookup_still_counts_against_the_budget(conn):
    cache, raw, _ = make(conn, raw=HubRaw())
    raw.fail = True

    _, stats = run_enrichment(cache, [two_leg("A1", "B1"), two_leg("A2", "B2"), two_leg("A3", "B3")],
                              max_lookups_per_direction=2)

    assert stats.lookups == 2 and stats.budget_reached and stats.unverifiable == 2


def test_search_results_cached_before_stops_existed_still_load():
    old_style = award_to_dict(award())
    del old_style["stops"]

    assert award_from_dict(old_style).stops is None
