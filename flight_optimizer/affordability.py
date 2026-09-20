"""Rank itineraries by what the owner's balances can actually pay for.

Model: each program's own balance is used first. Any shortfall is covered by a
Capital One transfer, if that program is a verified transfer partner. All
transfers draw on the one shared Capital One balance.
"""

import math
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Sequence

from flight_optimizer.itineraries import Itinerary, TripSearchResult

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

# Capital One miles needed per program point. Only partners confirmed as 1:1 are
# listed. Virgin Atlantic (Capital One lists "Virgin Red") and JetBlue (5:3) are
# deliberately left out until verified, so they must be paid from their own balance.
CAPITAL_ONE_TRANSFERS = {
    "aeroplan": Fraction(1),
    "lifemiles": Fraction(1),
    "flyingblue": Fraction(1),
    "british": Fraction(1),
    "qantas": Fraction(1),
}

# Capital One's stated minimum transfer. We assume 1,000-mile steps, which is
# the conservative reading (it never under-counts what you'd have to move).
TRANSFER_INCREMENT = 1_000


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
    def self_transfers(self) -> int:
        """Total connections across all directions that would be separate bookings."""
        return sum(i.self_transfers for i in self.itineraries)


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


def _sort_key(option: TripOption) -> tuple:
    a = option.affordability
    if a.affordable:
        # Fewest self-transfers first: one ticket means bags are checked through and
        # the airline covers a missed connection. Then cheapest.
        return (0, option.self_transfers, option.total_miles, a.capital_one_used)
    return (1, a.total_shortfall, option.total_miles)


def rank_trip(result: TripSearchResult, balances: dict[str, int]) -> list[TripOption]:
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
    ]
    return sorted(options, key=_sort_key)
