"""What the owner wants from each trip, and which alerts that allows (owner's rule, 2026-10-03).

Two goals, set per trip with `trip goal`; a trip has one or both:

- cheapest: find the cheapest way to fly. Covers economy.
- upgrade: notice when a better seat is worth it. Covers premium economy and business.

Each goal has an optional max, in dollars for the round trip, per cabin (`cheapest_max` for economy,
`premium_economy_max`, `business_max`). A max is an ALERT THRESHOLD, not a filter: every cabin is still
searched, priced and reported, but an alert for a cabin fires only when its cost is at or under that
cabin's max, and being at or under it is itself news (it replaced the old per-cabin cash targets). An award
is costed the way the cabin comparison does it: miles at `cabins.MILE_VALUE_CENTS` plus taxes and fees.

The upgrade goal can also flag a better cabin that costs at most `upgrade_multiple` (X) times economy, the
cheapest way to fly (`cabins.compare_cabins`). X is relative and never fires above a set cabin max. None
turns the flag off.

A trip with no saved goals gets `TripGoals()`: both goals, no maxes, X = `DEFAULT_UPGRADE_MULTIPLE`,
which is how every trip behaved before goals existed.
"""

import sqlite3
from dataclasses import dataclass, replace

from flight_optimizer.seats_aero import Cabin

DEFAULT_UPGRADE_MULTIPLE = 1.5  # accepted by the owner (2026-09-21) as the default X

UPGRADE_CABINS = (Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS)


@dataclass(frozen=True)
class TripGoals:
    cheapest: bool = True
    upgrade: bool = True
    cheapest_max: int | None = None  # economy, round trip, dollars
    premium_economy_max: int | None = None
    business_max: int | None = None
    upgrade_multiple: float | None = DEFAULT_UPGRADE_MULTIPLE  # None: no relative flag

    def __post_init__(self):
        if not (self.cheapest or self.upgrade):
            raise ValueError("A trip needs at least one goal. To stop all alerts for it, make it inactive instead.")
        for name in ("cheapest_max", "premium_economy_max", "business_max"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name.replace('_', ' ')} must be more than $0.")
        if self.upgrade_multiple is not None and self.upgrade_multiple <= 1:
            raise ValueError("The upgrade multiple must be more than 1 (a better cabin costs more than economy).")

    def covers(self, cabin: Cabin) -> bool:
        """True when alerts about `cabin` serve one of the trip's goals."""
        if cabin is Cabin.ECONOMY:
            return self.cheapest
        return self.upgrade and cabin in UPGRADE_CABINS

    def max_for(self, cabin: Cabin) -> int | None:
        """The most the owner would pay for `cabin`, round trip, or None for no ceiling."""
        return {
            Cabin.ECONOMY: self.cheapest_max,
            Cabin.PREMIUM_ECONOMY: self.premium_economy_max,
            Cabin.BUSINESS: self.business_max,
        }.get(cabin)

    def within_max(self, cabin: Cabin, cost: float | None) -> bool:
        """True when there is no max for `cabin`, or `cost` is at or under it."""
        ceiling = self.max_for(cabin)
        return ceiling is None or (cost is not None and cost <= ceiling)

    @property
    def flags_upgrades(self) -> bool:
        return self.upgrade and self.upgrade_multiple is not None

    def describe(self) -> str:
        """One line, e.g. "cheapest (max $600); upgrade: business max $2,000, flag at 1.5x economy"."""
        parts = []
        if self.cheapest:
            parts.append("cheapest" + (f" (max ${self.cheapest_max:,})" if self.cheapest_max else ""))
        if self.upgrade:
            details = [
                f"{label} max ${value:,}"
                for label, value in (("premium economy", self.premium_economy_max), ("business", self.business_max))
                if value is not None
            ]
            details.append(
                f"flag at {self.upgrade_multiple:g}x economy" if self.upgrade_multiple is not None
                else "no multiple flag"
            )
            parts.append("upgrade: " + ", ".join(details))
        return "; ".join(parts)


_COLUMNS = ("cheapest", "upgrade", "cheapest_max", "premium_economy_max", "business_max", "upgrade_multiple")


def load_goals(conn: sqlite3.Connection, trip_id: int) -> TripGoals:
    """The trip's goals, or the defaults when none were saved."""
    row = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM trip_goals WHERE trip_id = ?", (trip_id,)).fetchone()
    if row is None:
        return TripGoals()
    return TripGoals(
        cheapest=bool(row["cheapest"]), upgrade=bool(row["upgrade"]), cheapest_max=row["cheapest_max"],
        premium_economy_max=row["premium_economy_max"], business_max=row["business_max"],
        upgrade_multiple=row["upgrade_multiple"],
    )


def save_goals(conn: sqlite3.Connection, trip_id: int, goals: TripGoals) -> None:
    values = (
        int(goals.cheapest), int(goals.upgrade), goals.cheapest_max, goals.premium_economy_max,
        goals.business_max, goals.upgrade_multiple,
    )
    with conn:
        conn.execute(
            f"INSERT INTO trip_goals (trip_id, {', '.join(_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?)"
            f" ON CONFLICT (trip_id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS),
            (trip_id, *values),
        )


def update_goals(conn: sqlite3.Connection, trip_id: int, **changes) -> TripGoals:
    """Change some of a trip's goals (validated as a whole) and save them."""
    goals = replace(load_goals(conn, trip_id), **changes)
    save_goals(conn, trip_id, goals)
    return goals
