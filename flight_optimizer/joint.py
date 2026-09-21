"""Check trips against each other: they draw on the same balances.

Each trip is analyzed alone, so two trips can each be told "you can afford this" when together they
cannot (both may need the same Capital One miles). The rule for what counts: only options that would
themselves be alert-worthy (affordable, data not stale, worth more as miles than as cash), the leading
`JOINT_TOP` of each cabin, and only trips looked at in pairs. `affordability.assess` already takes several
itineraries, so a pair is assessed by pooling both options' itineraries.

Nothing here says which trip should get the miles: it shows what fits together and what does not. Notes
written for alerts never state balances.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from itertools import combinations

from flight_optimizer.affordability import Affordability, TripOption, assess
from flight_optimizer.alerts import usable_for_alert
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.cabins import cabin_label
from flight_optimizer.seats_aero import Cabin

JOINT_TOP = 5  # leading options per cabin, per trip

_CABIN_ORDER = {Cabin.ECONOMY: 0, Cabin.PREMIUM_ECONOMY: 1, Cabin.BUSINESS: 2, Cabin.FIRST: 3}


@dataclass(frozen=True)
class Candidate:
    trip: sqlite3.Row
    cabin: Cabin
    option: TripOption


@dataclass(frozen=True)
class Pair:
    first: Candidate
    second: Candidate
    affordability: Affordability  # of both together, on the shared balances

    @property
    def total_miles(self) -> int:
        return self.first.option.total_miles + self.second.option.total_miles


def candidates(analyses: list[TripAnalysis], today: date) -> list[Candidate]:
    """One trip's alert-worthy options, leading ones first in each cabin."""
    found = []
    for analysis in sorted(analyses, key=lambda a: _CABIN_ORDER[a.cabin]):
        for option in usable_for_alert(analysis, today)[:JOINT_TOP]:
            found.append(Candidate(analysis.trip, analysis.cabin, option))
    return found


def together(first: TripOption, second: TripOption, balances: dict[str, int]) -> Affordability:
    return assess([*first.itineraries, *second.itineraries], balances)


@dataclass(frozen=True)
class Narrowing:
    """Booking `candidate` by award leaves `other` only these cabins (an empty tuple: nothing) affordable."""

    candidate: Candidate
    other: sqlite3.Row
    kept: tuple[Cabin, ...]


@dataclass(frozen=True)
class TripPair:
    """Two trips, and how their options fit on the shared balances."""

    first: sqlite3.Row
    second: sqlite3.Row
    first_candidates: tuple[Candidate, ...]
    second_candidates: tuple[Candidate, ...]
    best: Pair | None  # the fewest total miles among pairs that are affordable together
    narrowings: tuple[Narrowing, ...]  # only where booking one leaves the other fewer cabins than it had

    @property
    def competing(self) -> bool:
        """Both trips have affordable options, so they can compete."""
        return bool(self.first_candidates and self.second_candidates)

    @property
    def conflict(self) -> bool:
        """Both trips have affordable options, and no pair of them is affordable together."""
        return self.competing and self.best is None


@dataclass(frozen=True)
class JointView:
    trips: dict[int, sqlite3.Row]
    by_trip: dict[int, tuple[Candidate, ...]]
    balances: dict[str, int]
    pairs: tuple[TripPair, ...]

    def notes_for(self, trip_id: int, option: TripOption) -> list[str]:
        """Lines for an alert or plan about `option` of `trip_id`: what it leaves the other trips. Nothing
        when it leaves them all their options. Never states balances."""
        notes = []
        for other_id, others in self.by_trip.items():
            if other_id == trip_id or not others:
                continue
            fits = [c for c in others if together(option, c.option, self.balances).affordable]
            name = self.trips[other_id]["name"]
            if not fits:
                notes.append(f"Shared miles: with this booked, nothing affordable is left for {name}.")
                continue
            kept = _cabins(fits)
            if kept != _cabins(others):
                notes.append(f"Shared miles: with this booked, {name} keeps only {_join(kept)} by award.")
        return notes


def _cabins(items: list[Candidate] | tuple[Candidate, ...]) -> tuple[Cabin, ...]:
    return tuple(sorted({c.cabin for c in items}, key=_CABIN_ORDER.get))


def _join(cabins: tuple[Cabin, ...]) -> str:
    return " and ".join(cabin_label(c) for c in cabins)


def _narrowings(
    mine: tuple[Candidate, ...], theirs: tuple[Candidate, ...], other: sqlite3.Row, balances: dict[str, int]
) -> list[Narrowing]:
    """For the leading option of each of my cabins (five near-identical ones would say the same thing
    five times): which of the other trip's cabins stay affordable alongside it."""
    found, seen = [], set()
    for candidate in mine:
        if candidate.cabin in seen:
            continue
        seen.add(candidate.cabin)
        kept = _cabins([t for t in theirs if together(candidate.option, t.option, balances).affordable])
        if kept != _cabins(theirs):
            found.append(Narrowing(candidate, other, kept))
    return found


def build_joint(groups: list[list[TripAnalysis]], balances: dict[str, int], today: date) -> JointView:
    """`groups` are each trip's per-cabin analyses."""
    trips = {g[0].trip["id"]: g[0].trip for g in groups}
    by_trip = {g[0].trip["id"]: tuple(candidates(g, today)) for g in groups}

    pairs = []
    for a_id, b_id in combinations(by_trip, 2):
        first, second = by_trip[a_id], by_trip[b_id]
        best = None
        for a in first:
            for b in second:
                result = together(a.option, b.option, balances)
                if result.affordable:
                    pair = Pair(a, b, result)
                    if best is None or pair.total_miles < best.total_miles:
                        best = pair
        narrowings = []
        if first and second:
            narrowings = _narrowings(first, second, trips[b_id], balances) + _narrowings(second, first, trips[a_id], balances)
        pairs.append(TripPair(trips[a_id], trips[b_id], first, second, best, tuple(narrowings)))
    return JointView(trips=trips, by_trip=by_trip, balances=balances, pairs=tuple(pairs))
