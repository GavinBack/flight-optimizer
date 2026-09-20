"""Connection-time rules for two-leg itineraries.

Owner's rules, applied to every hub connection:
- Prefer a same-day connection with at least 2.5 hours between flights.
- A same-day connection over 1 hour but under 2.5 hours is allowed, with a warning.
- Never recommend a connection of 1 hour or less.
- An overnight stop is allowed but ranks below any same-day connection.

Times are local to the hub (arrival and departure are at the same airport), so
plain subtraction is correct.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from flight_optimizer.seats_aero import FlightOption

MIN_CONNECTION_MINUTES = 60  # at or below this: never recommended
COMFORTABLE_CONNECTION_MINUTES = 150  # 2.5 hours; below this (same day) warns


class ConnectionClass(IntEnum):
    """Lower is better. The value is the ranking penalty."""

    COMFORTABLE = 0  # same day, at least 2.5 hours
    TIGHT = 1  # same day, over 1 hour but under 2.5 hours: warn
    OVERNIGHT = 2  # arrives one day, departs another
    UNSAFE = 3  # 1 hour or less (or departs before arriving): never recommended


def connection_minutes(arrives_at: datetime, departs_at: datetime) -> int:
    return int((departs_at - arrives_at).total_seconds() // 60)


def classify(arrives_at: datetime, departs_at: datetime) -> ConnectionClass:
    minutes = connection_minutes(arrives_at, departs_at)
    if minutes <= MIN_CONNECTION_MINUTES:
        return ConnectionClass.UNSAFE
    if departs_at.date() != arrives_at.date():
        return ConnectionClass.OVERNIGHT
    if minutes < COMFORTABLE_CONNECTION_MINUTES:
        return ConnectionClass.TIGHT
    return ConnectionClass.COMFORTABLE


@dataclass(frozen=True)
class Pairing:
    first: FlightOption
    second: FlightOption
    connection: ConnectionClass
    minutes: int

    @property
    def miles(self) -> int:
        return self.first.miles + self.second.miles


def best_pairing(
    first_options: list[FlightOption], second_options: list[FlightOption]
) -> Pairing | None:
    """The best acceptable way to join two legs, or None if every pairing is unsafe.

    Best = best connection class first (same day and comfortable beats tight beats
    overnight), then fewest miles, then the longest buffer.
    """
    best: Pairing | None = None
    best_key: tuple | None = None
    for first in first_options:
        for second in second_options:
            klass = classify(first.arrives_at, second.departs_at)
            if klass is ConnectionClass.UNSAFE:
                continue
            minutes = connection_minutes(first.arrives_at, second.departs_at)
            key = (klass, first.miles + second.miles, -minutes)
            if best_key is None or key < best_key:
                best, best_key = Pairing(first, second, klass, minutes), key
    return best
