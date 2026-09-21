"""Compare a trip across cabins, and spot when a better cabin is worth its price.

The owner's goal is the best price for each trip, and to notice when business (or premium
economy) costs little enough to be worth taking instead of economy. To compare a cash fare with
an award on one scale, an award is costed as its miles times `MILE_VALUE_CENTS`, before taxes
and fees (the rest of the app treats award figures the same way).

Both numbers below were accepted by the owner (2026-09-21); revisit them if the owner's goals change:

- `MILE_VALUE_CENTS`: what a mile is worth. 1.0 is Capital One's travel-eraser rate, the
  floor a Capital One mile can always be turned into cash at.
- `UPGRADE_MAX_MULTIPLE`: a better cabin is an opportunity when it costs no more than this many
  times the cheapest way to fly the trip.

Only awards you can already afford, on data that is not stale, count. What a better cabin earns
in miles and status is not modeled: the owner is not chasing a specific program, so it would only
make upgrades look cheaper than they are here.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

from flight_optimizer.affordability import TripOption
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.itineraries import Freshness
from flight_optimizer.seats_aero import Cabin

MILE_VALUE_CENTS = 1.0
UPGRADE_MAX_MULTIPLE = 1.5

_RANK = {Cabin.ECONOMY: 0, Cabin.PREMIUM_ECONOMY: 1, Cabin.BUSINESS: 2, Cabin.FIRST: 3}


def cabin_label(cabin: Cabin) -> str:
    return cabin.name.lower().replace("_", " ")


@dataclass(frozen=True)
class CabinOutlook:
    """The cheapest way to fly one cabin right now, by cash or by an affordable award."""

    cabin: Cabin
    cash: int | None  # cheapest reasonable cash round trip
    award: TripOption | None  # the cheapest (in miles) affordable award whose data is not stale
    award_cost: float | None  # its miles at MILE_VALUE_CENTS, in dollars

    @property
    def best_cost(self) -> float | None:
        costs = [c for c in (self.cash, self.award_cost) if c is not None]
        return min(costs) if costs else None

    @property
    def best_way(self) -> str | None:
        if self.best_cost is None:
            return None
        return "award" if self.award_cost is not None and self.award_cost == self.best_cost else "cash"

    @property
    def offered(self) -> bool:
        return self.cash is not None or self.award is not None


@dataclass(frozen=True)
class Upgrade:
    """A better cabin that costs little more than the cheapest way to fly the trip."""

    outlook: CabinOutlook
    over: CabinOutlook  # the cheapest cabin it is compared with
    extra: float  # dollars more than `over`
    multiple: float


@dataclass(frozen=True)
class CabinComparison:
    trip: sqlite3.Row
    outlooks: tuple[CabinOutlook, ...]  # in cabin order, economy first
    upgrades: tuple[Upgrade, ...]

    @property
    def cheapest(self) -> CabinOutlook | None:
        priced = [o for o in self.outlooks if o.best_cost is not None]
        return min(priced, key=lambda o: (o.best_cost, _RANK[o.cabin]), default=None)


def outlook_for(analysis: TripAnalysis, today: date) -> CabinOutlook:
    usable = [o for o in analysis.affordable if o.freshness(today) is not Freshness.STALE]
    award = min(usable, key=lambda o: o.total_miles) if usable else None  # a cost comparison: the fewest miles
    return CabinOutlook(
        cabin=analysis.cabin,
        cash=analysis.baseline.cabin_total if analysis.baseline is not None else None,
        award=award,
        award_cost=None if award is None else award.total_miles * MILE_VALUE_CENTS / 100,
    )


def compare_cabins(analyses: list[TripAnalysis], today: date) -> CabinComparison:
    """`analyses` are one trip's per-cabin analyses."""
    outlooks = tuple(sorted((outlook_for(a, today) for a in analyses), key=lambda o: _RANK[o.cabin]))
    comparison = CabinComparison(trip=analyses[0].trip, outlooks=outlooks, upgrades=())
    base = comparison.cheapest
    if base is None:
        return comparison
    upgrades = tuple(
        Upgrade(outlook=o, over=base, extra=o.best_cost - base.best_cost, multiple=o.best_cost / base.best_cost)
        for o in outlooks
        if _RANK[o.cabin] > _RANK[base.cabin]
        and o.best_cost is not None
        and o.best_cost <= base.best_cost * UPGRADE_MAX_MULTIPLE
    )
    return CabinComparison(trip=comparison.trip, outlooks=outlooks, upgrades=upgrades)
