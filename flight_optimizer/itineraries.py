"""Search a trip through gateway hubs and combine the legs into itineraries.

Seats.aero only indexes airport pairs that programs publish and never stitches
flights together, so a pair like RNO->SJO returns nothing. We search
origin->hub and hub->destination separately and pair them ourselves.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum

from flight_optimizer import connections, db
from flight_optimizer.connections import ConnectionClass
from flight_optimizer.seats_aero import AwardResult, Cabin, SeatsAeroClient


# Legs reporting fewer seats than this are treated as unbookable. Set to 0 to keep them.
MIN_SEATS = 1
# Every trip is priced in these cabins (the owner's rule). Premium economy simply finds nothing
# on routes that don't sell it. First is left out: the goal is economy, and upgrades to business.
EVALUATED_CABINS = (Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS)

# Owner's rule: offers and availability should be current. Award data is judged by its age in
# days, and an option by its stalest leg (an option is only as reliable as its weakest leg).
CURRENT_AWARD_DAYS = 3  # this fresh or fresher is "current"
STALE_AWARD_DAYS = 7  # older than this is "stale": ranked last, and never alerted on


class Freshness(IntEnum):
    """Lower is better. The value is the ranking penalty."""

    CURRENT = 0
    AGING = 1
    STALE = 2


def leg_age_days(leg: AwardResult, today: date) -> int | None:
    """Days since Seats.aero last refreshed this leg, or None if unknown."""
    if not leg.updated_at:
        return None
    return (today - date.fromisoformat(leg.updated_at[:10])).days


def leg_freshness(leg: AwardResult, today: date) -> Freshness:
    age = leg_age_days(leg, today)
    if age is None:
        return Freshness.AGING  # can't confirm it is current, but nothing says it is stale
    if age <= CURRENT_AWARD_DAYS:
        return Freshness.CURRENT
    return Freshness.AGING if age <= STALE_AWARD_DAYS else Freshness.STALE


def is_stale(leg: AwardResult, today: date) -> bool:
    return leg_freshness(leg, today) is Freshness.STALE


@dataclass(frozen=True)
class Itinerary:
    """One direct result, or two legs joined at a gateway hub.

    Legs can be in different programs, which means separate award bookings.
    """

    legs: tuple[AwardResult, ...]
    via: str | None  # gateway airport, or None for a direct pair

    @property
    def total_miles(self) -> int:
        """Sum across legs. With mixed programs this is not a single balance's cost."""
        return sum(leg.miles for leg in self.legs)

    @property
    def self_transfers(self) -> int:
        """Connections that would be separate bookings: 0 if this could be one ticket.

        A heuristic, not a guarantee. It counts as one ticket only when every leg is in
        the same program and the connection is the same day (an overnight stay is a
        stopover, which a program may not sell as one ticket). Even then, the program
        may price the combined itinerary differently from the sum of its legs, so
        verify on the program's own site. Separate tickets mean bags are re-checked
        and the airline isn't responsible if a late first leg misses the second.
        """
        if len(self.legs) < 2:
            return 0
        first, second = self.legs
        same_ticket = first.source == second.source and first.date == second.date
        return 0 if same_ticket else 1

    @property
    def hub_leg_stops(self) -> int:
        """Stops inside the two legs of a hub itinerary (0 for a single-leg itinerary, where
        stops are inherent to the trip, or when the stops are not known yet)."""
        if len(self.legs) != 2:
            return 0
        return sum(leg.stops or 0 for leg in self.legs)

    def freshness(self, today: date) -> Freshness:
        """The stalest of this itinerary's legs."""
        return max(leg_freshness(leg, today) for leg in self.legs)

    def oldest_age_days(self, today: date) -> int | None:
        ages = [age for leg in self.legs if (age := leg_age_days(leg, today)) is not None]
        return max(ages) if ages else None

    def stale_legs(self, today: date) -> int:
        """How many legs have award data older than STALE_AWARD_DAYS."""
        return sum(1 for leg in self.legs if is_stale(leg, today))

    @property
    def connection_minutes(self) -> int | None:
        """Time at the hub between the legs, or None if there is no connection or the
        flight times haven't been looked up."""
        if len(self.legs) != 2:
            return None
        first, second = self.legs
        if first.arrives_at is None or second.departs_at is None:
            return None
        return connections.connection_minutes(first.arrives_at, second.departs_at)

    @property
    def connection_class(self) -> ConnectionClass | None:
        """None when there is no connection or the times are unknown."""
        if self.connection_minutes is None:
            return None
        first, second = self.legs
        return connections.classify(first.arrives_at, second.departs_at)

    @property
    def miles_by_program(self) -> dict[str, int]:

        totals: dict[str, int] = {}
        for leg in self.legs:
            totals[leg.source] = totals.get(leg.source, 0) + leg.miles
        return totals


@dataclass(frozen=True)
class TripSearchResult:
    trip_id: int
    outbound: list[Itinerary]
    return_trip: list[Itinerary] | None  # None for a one-way trip


def combine_via_gateway(
    first_legs: list[AwardResult],
    second_legs: list[AwardResult],
    gateway: str,
    max_layover_days: int = 1,
) -> list[Itinerary]:
    """Pair every first leg with every second leg departing 0..max_layover_days later."""
    return [
        Itinerary(legs=(first, second), via=gateway)
        for first in first_legs
        for second in second_legs
        if 0 <= (second.date - first.date).days <= max_layover_days
    ]


def search_one_way(
    client: SeatsAeroClient,
    origin: str,
    destination: str,
    on_date: date,
    *,
    gateways: list[str],
    cabin: Cabin,
    max_layover_days: int = 1,
    sources: list[str] | None = None,
    min_seats: int = MIN_SEATS,
) -> list[Itinerary]:
    """Cheapest-first itineraries leaving `origin` on `on_date`, direct or via a gateway.

    Legs reporting fewer than `min_seats` seats are dropped as unbookable. Some programs
    (Azul, Aeromexico, mostly American) report 0 seats on most results, which may mean
    "count unknown", so this rule can exclude real availability.

    Costs one API call for the direct pair plus one or two per gateway. The second
    leg is only searched if the first leg found something.
    """

    def leg(frm: str, to: str, start: date, end: date) -> list[AwardResult]:
        results = client.search(frm, to, start, end, sources=sources)
        return [r for r in results if r.cabin is cabin and r.remaining_seats >= min_seats]

    itineraries = [
        Itinerary(legs=(r,), via=None)
        for r in leg(origin, destination, on_date, on_date)
    ]

    latest = on_date + timedelta(days=max_layover_days)
    for gateway in gateways:
        if gateway in (origin, destination):
            continue
        first_legs = leg(origin, gateway, on_date, on_date)
        if not first_legs:
            continue
        second_legs = leg(gateway, destination, on_date, latest)
        itineraries += combine_via_gateway(first_legs, second_legs, gateway, max_layover_days)

    return sorted(itineraries, key=lambda i: i.total_miles)


class _SearchMemo:
    """Wraps a client so identical searches are paid for once.

    One Seats.aero search returns every cabin, so searching a trip cabin by cabin would repeat
    the same calls. Everything but `search` is passed through to the real client.
    """

    def __init__(self, client: SeatsAeroClient):
        self._client = client
        self._results: dict[tuple, list[AwardResult]] = {}

    def search(self, origin, destination, start_date, end_date, *, sources=None, **options):
        key = (origin, destination, start_date, end_date, tuple(sources or ()), tuple(sorted(options.items())))
        if key not in self._results:
            self._results[key] = self._client.search(
                origin, destination, start_date, end_date, sources=sources, **options
            )
        return self._results[key]

    def __getattr__(self, name):
        return getattr(self._client, name)


def search_trip(
    client: SeatsAeroClient,
    trip: sqlite3.Row,
    gateways: list[str],
    *,
    cabin: Cabin,
    max_layover_days: int = 1,
    sources: list[str] | None = None,
    min_seats: int = MIN_SEATS,
) -> TripSearchResult:
    options = dict(
        gateways=gateways, cabin=cabin, max_layover_days=max_layover_days,
        sources=sources, min_seats=min_seats,
    )

    outbound = search_one_way(
        client, trip["origin"], trip["destination"],
        date.fromisoformat(trip["depart_date"]), **options,
    )
    return_trip = None
    if trip["return_date"]:
        return_trip = search_one_way(
            client, trip["destination"], trip["origin"],
            date.fromisoformat(trip["return_date"]), **options,
        )
    return TripSearchResult(trip_id=trip["id"], outbound=outbound, return_trip=return_trip)


def search_trip_cabins(
    client: SeatsAeroClient,
    trip: sqlite3.Row,
    gateways: list[str],
    *,
    cabins: tuple[Cabin, ...] = EVALUATED_CABINS,
    **options,
) -> dict[Cabin, TripSearchResult]:
    """The trip searched in every cabin, for the API calls of one search (see `_SearchMemo`)."""
    memo = _SearchMemo(client)
    return {cabin: search_trip(memo, trip, gateways, cabin=cabin, **options) for cabin in cabins}


def search_active_trips(
    conn: sqlite3.Connection, client: SeatsAeroClient, **options
) -> dict[int, dict[Cabin, TripSearchResult]]:
    """Search every active trip in every evaluated cabin, and only active trips (see `db.active_trips`)."""
    return {
        trip["id"]: search_trip_cabins(client, trip, db.get_gateways(conn, trip["id"]), **options)
        for trip in db.active_trips(conn)
    }
