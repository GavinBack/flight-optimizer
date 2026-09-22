"""Attach flight times to the leading candidates and enforce the connection rules.

A Seats.aero search result is one row per route, date and program, with the
cheapest price and no times. The `trips` lookup (one API call per row) lists the
actual flights behind it. For two-leg itineraries we pick the best pairing of
flights that respects the connection rules, and use *that* pairing's mileage,
which can be higher than the cheapest row price.

Only the leading candidates in each direction are looked up, to protect the
1,000-a-day quota. Anything dropped here is counted, never silently ignored.
"""

from dataclasses import dataclass, replace
from datetime import date

from flight_optimizer.affordability import assess
from flight_optimizer.connections import best_pairing
from flight_optimizer.itineraries import MIN_SEATS, Itinerary, TripSearchResult
from flight_optimizer.seats_aero import (
    AwardResult,
    Cabin,
    FlightOption,
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroError,
    SeatsAeroRateLimitError,
    TripDetails,
)

DEFAULT_PER_DIRECTION = 12  # itineraries kept per direction after checking
DEFAULT_MAX_LOOKUPS_PER_DIRECTION = 20  # API calls allowed per direction


@dataclass
class EnrichmentStats:
    lookups: int = 0  # real API calls made (cache hits are not counted)
    cache_hits: int = 0  # flight details reused from the local cache
    dropped_unsafe: int = 0  # every flight pairing had a connection of 1 hour or less
    unverifiable: int = 0  # no flight details, or no bookable flights in the cabin
    wrong_landing_day: int = 0  # an outbound whose flights all land outside the days asked for
    budget_reached: bool = False  # stopped early to protect the API quota


def _details(
    client: SeatsAeroClient,
    leg: AwardResult,
    cache: dict[str, TripDetails | None],
    stats: EnrichmentStats,
) -> TripDetails | None:
    if leg.availability_id in cache:
        return cache[leg.availability_id]
    try:
        # The row's UpdatedAt is the version: an unchanged one lets a caching client
        # reuse the flights without an API call.
        details = client.trips(leg.availability_id, version=leg.updated_at)
    except (SeatsAeroAuthError, SeatsAeroRateLimitError):
        raise  # not this itinerary's problem: stop the whole run
    except SeatsAeroError:
        stats.lookups += 1  # the failed call still spent quota
        details = None
    else:
        if details.from_cache:
            stats.cache_hits += 1
        else:
            stats.lookups += 1
    cache[leg.availability_id] = details
    return details


def _usable(details: TripDetails, cabin: Cabin, min_seats: int) -> list[FlightOption]:
    return [o for o in details.options if o.cabin is cabin and o.seats >= min_seats]


def _with_flight(leg: AwardResult, option: FlightOption, details: TripDetails) -> AwardResult:
    """The leg as it would actually be booked: that flight's price, seats and times."""
    return replace(
        leg,
        miles=option.miles,
        remaining_seats=option.seats,
        direct=option.stops == 0,
        stops=option.stops,
        total_taxes=option.total_taxes,
        currency=option.currency or leg.currency,
        updated_at=option.updated_at or leg.updated_at,
        departs_at=option.departs_at,
        arrives_at=option.arrives_at,
        flight_numbers=option.flight_numbers,
        booking_url=details.booking_url,
    )


def enrich_itinerary(
    client: SeatsAeroClient,
    itinerary: Itinerary,
    *,
    cabin: Cabin,
    cache: dict[str, TripDetails | None],
    stats: EnrichmentStats,
    min_seats: int = MIN_SEATS,
    lands: tuple[date, date] | None = None,
) -> Itinerary | None:
    """The itinerary with real flights chosen, or None if it can't be recommended.

    With `lands`, only flights on the last leg that land (local time) within those days count,
    and the filter comes before choosing: a row can hold a daytime flight landing a day early
    and a red-eye landing on the day.
    """
    details: list[TripDetails] = []
    for leg in itinerary.legs:
        found = _details(client, leg, cache, stats) if leg.availability_id else None
        if found is None:
            stats.unverifiable += 1
            return None
        details.append(found)

    usable = [_usable(d, cabin, min_seats) for d in details]
    if any(not options for options in usable):
        stats.unverifiable += 1
        return None
    if lands is not None:
        usable[-1] = [o for o in usable[-1] if lands[0] <= o.arrives_at.date() <= lands[1]]
        if not usable[-1]:
            stats.wrong_landing_day += 1
            return None

    if len(itinerary.legs) == 1:
        option = min(usable[0], key=lambda o: (o.miles, o.stops))
        return replace(itinerary, legs=(_with_flight(itinerary.legs[0], option, details[0]),))

    pairing = best_pairing(usable[0], usable[1])
    if pairing is None:
        stats.dropped_unsafe += 1
        return None
    first, second = itinerary.legs
    return replace(
        itinerary,
        legs=(
            _with_flight(first, pairing.first, details[0]),
            _with_flight(second, pairing.second, details[1]),
        ),
    )


def _candidate_key(itinerary: Itinerary, balances: dict[str, int]) -> tuple:
    """Look up the likeliest winners first: affordable, fewest self-transfers, cheapest."""
    affordability = assess([itinerary], balances)
    if affordability.affordable:
        return (0, itinerary.self_transfers, itinerary.total_miles)
    return (1, affordability.total_shortfall, itinerary.total_miles)


def enrich_result(
    client: SeatsAeroClient,
    result: TripSearchResult,
    *,
    cabin: Cabin,
    balances: dict[str, int],
    per_direction: int = DEFAULT_PER_DIRECTION,
    max_lookups_per_direction: int = DEFAULT_MAX_LOOKUPS_PER_DIRECTION,
    min_seats: int = MIN_SEATS,
) -> tuple[TripSearchResult, EnrichmentStats]:
    """Replace each direction's itineraries with the verified leading candidates."""
    stats = EnrichmentStats()
    cache: dict[str, TripDetails | None] = {}

    def verified(itineraries: list[Itinerary], lands: tuple[date, date] | None = None) -> list[Itinerary]:
        ordered = sorted(itineraries, key=lambda it: _candidate_key(it, balances))
        limit = stats.lookups + max_lookups_per_direction
        kept: list[Itinerary] = []
        for itinerary in ordered:
            if len(kept) >= per_direction:
                break
            if stats.lookups >= limit:
                stats.budget_reached = True
                break
            enriched = enrich_itinerary(
                client, itinerary, cabin=cabin, cache=cache, stats=stats, min_seats=min_seats, lands=lands
            )
            if enriched is not None:
                kept.append(enriched)
        return kept

    outbound = verified(result.outbound, result.outbound_lands)
    return_trip = verified(result.return_trip) if result.return_trip is not None else None
    return replace(result, outbound=outbound, return_trip=return_trip), stats
