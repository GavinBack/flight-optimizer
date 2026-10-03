"""Rank itineraries by what the owner's balances can actually pay for.

Model: each program's own balance is used first. Any shortfall is covered by a
Capital One transfer, if that program is a verified transfer partner. All
transfers draw on the one shared Capital One balance.
"""

import math
from dataclasses import dataclass
from fractions import Fraction
from datetime import date
from itertools import product
from typing import Sequence

from flight_optimizer.itineraries import Freshness, Itinerary, TripSearchResult
from flight_optimizer.windows import MIN_STAY_NIGHTS

# Seats.aero `source` id -> key used in the balances table.
SOURCE_TO_PROGRAM = {
    "united": "united",
    "american": "american",
    "delta": "delta",
    "alaska": "atmos",
    "qantas": "qantas",
    "aeroplan": "aeroplan",
    "lifemiles": "lifemiles",
    "flyingblue": "flyingblue",
    "british": "british",
    "virginatlantic": "virginatlantic",
    "jetblue": "jetblue",
}

# Capital One miles needed per program point (checked 2026-09-20 against Capital One's own
# partner page and upgradedpoints.com; they agree). 1:1 except JetBlue, where 5 Capital One
# miles buy 3 TrueBlue points. Virgin Atlantic is reached through Virgin Red, which links to
# Flying Club at 1:1; Virgin's own support pages could not be read, so that link is
# confirmed from Capital One's listing plus secondary sources only.
CAPITAL_ONE_TRANSFERS = {
    "aeroplan": Fraction(1),
    "lifemiles": Fraction(1),
    "flyingblue": Fraction(1),
    "british": Fraction(1),
    "qantas": Fraction(1),
    "virginatlantic": Fraction(1),
    "jetblue": Fraction(5, 3),
}

# How long a transfer takes to arrive. Not instant everywhere: a slow transfer can outlast
# the award space, so alerts and plans state it.
TRANSFER_TIMES = {
    "aeroplan": "almost instant",
    "lifemiles": "almost instant",
    "flyingblue": "almost instant",
    "british": "almost instant",
    "qantas": "up to 36 hours",
    "jetblue": "2 to 5 days",
    "virginatlantic": "about 1 business day",
}

# Extra guidance shown in the booking plan, where a program's mechanics are uncertain.
TRANSFER_NOTES = {
    "virginatlantic": (
        "Capital One sends this to your Virgin Red membership number (confirmed on the transfer screen). "
        "To book Virgin Atlantic flights with the points you also need a free Flying Club account, "
        "linked to Virgin Red so the points pool into one balance (linking is done in the Virgin Red app: "
        "Account > Link accounts > Link Virgin Atlantic)."
    ),
}

# Capital One states a 1,000-mile minimum but not the step size. One secondary source says
# 100-mile steps for Virgin; we assume 1,000, which can only over-count what you must move.
TRANSFER_INCREMENT = 1_000


def points_received(program: str, capital_one_miles: int) -> int:
    """Program points that arrive for a transfer (whole points, rounded down)."""
    return int(Fraction(capital_one_miles) / CAPITAL_ONE_TRANSFERS[program])


def ratio_text(program: str) -> str:
    rate = CAPITAL_ONE_TRANSFERS[program]
    return f"{rate.numerator}:{rate.denominator}"


def slow_transfers(option) -> dict[str, str]:
    """Transfers this option needs that are not near-instant, with how long they take."""
    return {
        program: TRANSFER_TIMES[program]
        for program in option.affordability.transfers
        if TRANSFER_TIMES.get(program, "almost instant") != "almost instant"
    }


@dataclass(frozen=True)
class Affordability:
    affordable: bool
    transfers: dict[str, int]  # program -> Capital One miles to move into it
    capital_one_used: int
    capital_one_short: int  # how far the transfers exceed the Capital One balance
    shortfalls: dict[str, int]  # program -> points short, where no transfer can help
    unknown_sources: tuple[str, ...]  # Seats.aero sources with no program mapping
    total_shortfall: int  # all of the above, in miles, for ranking near-misses


@dataclass(frozen=True)
class TripOption:
    """One outbound (and, for round trips, one return) with its affordability."""

    itineraries: tuple[Itinerary, ...]
    affordability: Affordability

    @property
    def total_miles(self) -> int:
        return sum(i.total_miles for i in self.itineraries)

    @property
    def depart_date(self) -> date:
        """The day the outbound's first flight leaves."""
        return self.itineraries[0].legs[0].date

    @property
    def arrive_date(self) -> date:
        """The day the outbound lands (local), or its last flight's day if times are not known."""
        return _lands(self.itineraries[0])

    @property
    def return_date(self) -> date | None:
        """The day the return's first flight leaves, for a round trip."""
        return self.itineraries[1].legs[0].date if len(self.itineraries) > 1 else None

    @property
    def self_transfers(self) -> int:
        """Total connections across all directions that would be separate bookings."""
        return sum(i.self_transfers for i in self.itineraries)

    @property
    def stop_penalty(self) -> int:
        """Extra flights inside the hub legs, summed over directions. Fewer is better."""
        return sum(i.hub_leg_stops for i in self.itineraries)

    def freshness(self, today: date) -> Freshness:
        """The stalest leg anywhere in the option."""
        return max(i.freshness(today) for i in self.itineraries)

    def oldest_age_days(self, today: date) -> int | None:
        ages = [age for i in self.itineraries if (age := i.oldest_age_days(today)) is not None]
        return max(ages) if ages else None

    def stale_leg_count(self, today: date) -> int:
        return sum(i.stale_legs(today) for i in self.itineraries)

    @property
    def connection_penalty(self) -> int:
        """0 comfortable same-day, 1 tight, 2 overnight, summed over directions.
        Unknown times count as 0 (they are flagged separately, never silently trusted)."""
        return sum(
            int(i.connection_class) for i in self.itineraries if i.connection_class is not None
        )


def assess(itineraries: Sequence[Itinerary], balances: dict[str, int]) -> Affordability:
    """Can `balances` pay for all of these itineraries together?"""
    needs: dict[str, int] = {}
    unknown_sources: set[str] = set()
    unknown_miles = 0
    for itinerary in itineraries:
        for leg in itinerary.legs:
            program = SOURCE_TO_PROGRAM.get(leg.source)
            if program is None:
                unknown_sources.add(leg.source)
                unknown_miles += leg.miles
            else:
                needs[program] = needs.get(program, 0) + leg.miles

    transfers: dict[str, int] = {}
    shortfalls: dict[str, int] = {}
    for program, need in needs.items():
        short = need - balances.get(program, 0)
        if short <= 0:
            continue
        rate = CAPITAL_ONE_TRANSFERS.get(program)
        if rate is None:
            shortfalls[program] = short
        else:
            steps = math.ceil(short * rate / TRANSFER_INCREMENT)
            transfers[program] = steps * TRANSFER_INCREMENT

    capital_one_used = sum(transfers.values())
    capital_one_short = max(0, capital_one_used - balances.get("capital_one", 0))
    total_shortfall = sum(shortfalls.values()) + capital_one_short + unknown_miles

    return Affordability(
        affordable=total_shortfall == 0,
        transfers=transfers,
        capital_one_used=capital_one_used,
        capital_one_short=capital_one_short,
        shortfalls=shortfalls,
        unknown_sources=tuple(sorted(unknown_sources)),
        total_shortfall=total_shortfall,
    )


def _sort_key(option: TripOption, today: date) -> tuple:
    a = option.affordability
    if a.affordable:
        # Current data first (offers and availability should be current), judged by the
        # stalest leg. Then fewest self-transfers: one ticket means bags are checked through and
        # the airline covers a missed connection. Then same-day connections with a
        # comfortable buffer over tight ones over overnight stops. Then fewer extra flights
        # inside the hub legs, then fresher award data. Then cheapest.
        return (
            0, int(option.freshness(today)), option.self_transfers, option.connection_penalty,
            option.stop_penalty, option.stale_leg_count(today), option.total_miles, a.capital_one_used,
        )
    return (1, a.total_shortfall, option.total_miles)


def _lands(itinerary: Itinerary) -> date:
    """The day an itinerary lands (local): its last flight's arrival, or that flight's day if times are unknown."""
    last = itinerary.legs[-1]
    return last.arrives_at.date() if last.arrives_at is not None else last.date


def rank_trip(
    result: TripSearchResult, balances: dict[str, int], today: date | None = None
) -> list[TripOption]:
    """Every full-trip combination, affordable ones first, then near-misses ordered
    by how little they're short.

    Among affordable options, fewer self-transfers wins over price (see
    `Itinerary.self_transfers`); within the same number, cheapest first.

    A round trip pairs each outbound with each return and assesses them together,
    so legs in the same program share one balance.
    """
    directions = [result.outbound]
    if result.return_trip is not None:
        directions.append(result.return_trip)

    options = [
        TripOption(itineraries=combo, affordability=assess(combo, balances))
        for combo in product(*directions)
        # At least MIN_STAY_NIGHTS between landing and flying home: with flexible dates an outbound can
        # otherwise land the day the return leaves, or after it.
        if len(combo) == 1 or (combo[1].legs[0].date - _lands(combo[0])).days >= MIN_STAY_NIGHTS
    ]
    today = today or date.today()
    return sorted(options, key=lambda option: _sort_key(option, today))
