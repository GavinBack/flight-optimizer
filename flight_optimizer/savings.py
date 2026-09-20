"""Value an award option against the cash baseline: cash replaced and cents per mile.

The comparison is against the trip's through fare (e.g. RNO>SJO as one cash ticket),
even though awards may be built from separate legs via a gateway hub. All figures
are before award taxes and fees, so they are upper bounds on value.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

from flight_optimizer import cash_fares
from flight_optimizer.affordability import TripOption
from flight_optimizer.cash_fares import CashFare, SerpApiClient
from flight_optimizer.seats_aero import Cabin


@dataclass(frozen=True)
class TripBaseline:
    """Baseline cash fares for each direction of a trip, in the award cabin and economy.

    A direction is None when no fare met the baseline limits.
    """

    cabin_fares: tuple[CashFare | None, ...]
    economy_fares: tuple[CashFare | None, ...]
    fetched_at: str | None = None  # the oldest search behind these fares (UTC ISO)

    @staticmethod
    def _total(fares: tuple[CashFare | None, ...]) -> int | None:
        if any(fare is None for fare in fares):
            return None
        return sum(fare.price for fare in fares)

    @property
    def cabin_total(self) -> int | None:
        return self._total(self.cabin_fares)

    @property
    def economy_total(self) -> int | None:
        return self._total(self.economy_fares)


@dataclass(frozen=True)
class ValueEstimate:
    cabin_cash: int | None  # baseline cash price, award cabin
    economy_cash: int | None  # baseline cash price, economy
    cents_per_mile_vs_cabin: float | None
    cents_per_mile_vs_economy: float | None
    upgrade_premium: int | None  # extra cash the premium cabin costs over economy


def trip_baseline(
    conn: sqlite3.Connection,
    client: SerpApiClient,
    trip: sqlite3.Row,
    *,
    max_layover_minutes: int = cash_fares.DEFAULT_MAX_LAYOVER_MINUTES,
    max_total_minutes: int = cash_fares.DEFAULT_MAX_TOTAL_MINUTES,
    **fare_options,
) -> TripBaseline:
    """Baselines for every direction of the trip. Uses cached searches where fresh.

    Costs up to 2 searches per direction (award cabin and economy), fewer if cached.
    """
    award_cabin = Cabin[trip["cabin"].upper()]
    directions = [(trip["origin"], trip["destination"], date.fromisoformat(trip["depart_date"]))]
    if trip["return_date"]:
        directions.append(
            (trip["destination"], trip["origin"], date.fromisoformat(trip["return_date"]))
        )

    fetched: list[str] = []

    def baseline_for(cabin: Cabin) -> tuple[CashFare | None, ...]:
        fares = []
        for origin, dest, on in directions:
            search = cash_fares.get_fares(conn, client, origin, dest, on, cabin, **fare_options)
            fetched.append(search.fetched_at)
            fares.append(
                cash_fares.cheapest_reasonable(
                    search.fares,
                    max_layover_minutes=max_layover_minutes,
                    max_total_minutes=max_total_minutes,
                )
            )
        return tuple(fares)

    cabin_fares, economy_fares = baseline_for(award_cabin), baseline_for(Cabin.ECONOMY)
    return TripBaseline(
        cabin_fares=cabin_fares,
        economy_fares=economy_fares,
        fetched_at=min(fetched) if fetched else None,  # the oldest search sets how current this is
    )


def estimate_value(option: TripOption, baseline: TripBaseline) -> ValueEstimate:
    """Cents per mile = cash replaced / miles x 100, before award taxes and fees."""
    miles = option.total_miles
    cabin_cash, economy_cash = baseline.cabin_total, baseline.economy_total

    def cents_per_mile(cash: int | None) -> float | None:
        return None if cash is None or miles == 0 else cash / miles * 100

    premium = None if cabin_cash is None or economy_cash is None else cabin_cash - economy_cash
    return ValueEstimate(
        cabin_cash=cabin_cash,
        economy_cash=economy_cash,
        cents_per_mile_vs_cabin=cents_per_mile(cabin_cash),
        cents_per_mile_vs_economy=cents_per_mile(economy_cash),
        upgrade_premium=premium,
    )
