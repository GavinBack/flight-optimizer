"""Everything needed to report or alert on one trip, computed once.

Search through the gateways, look up flight times for the leading candidates and
enforce the connection rules, rank against balances, and value against cash. The
report and the alerts both call `analyze_trip`, so they can never disagree.
"""

import sqlite3
from dataclasses import dataclass

from flight_optimizer import db
from flight_optimizer.affordability import TripOption, rank_trip
from flight_optimizer.cash_fares import SerpApiClient, SerpApiError
from flight_optimizer.enrichment import EnrichmentStats, enrich_result
from flight_optimizer.itineraries import search_trip
from flight_optimizer.savings import TripBaseline, trip_baseline
from flight_optimizer.seats_aero import Cabin, SeatsAeroClient


@dataclass(frozen=True)
class TripAnalysis:
    trip: sqlite3.Row
    gateways: list[str]
    ranked: list[TripOption]  # only options whose flight times were verified
    baseline: TripBaseline | None
    cash_note: str | None  # why there is no baseline, if there isn't
    stats: EnrichmentStats

    @property
    def affordable(self) -> list[TripOption]:
        return [option for option in self.ranked if option.affordability.affordable]


def analyze_trip(
    conn: sqlite3.Connection,
    awards: SeatsAeroClient,
    cash: SerpApiClient | None,
    trip: sqlite3.Row,
    balances: dict[str, int],
) -> TripAnalysis:
    gateways = db.get_gateways(conn, trip["id"])
    found = search_trip(awards, trip, gateways)
    verified, stats = enrich_result(
        awards, found, cabin=Cabin[trip["cabin"].upper()], balances=balances
    )
    ranked = rank_trip(verified, balances)

    baseline, note = None, None
    if cash is None:
        note = "Cash prices skipped: SERPAPI_API_KEY is not set."
    else:
        try:
            baseline = trip_baseline(conn, cash, trip)
        except SerpApiError as exc:
            note = f"Cash prices unavailable: {exc}"

    return TripAnalysis(
        trip=trip, gateways=gateways, ranked=ranked, baseline=baseline, cash_note=note, stats=stats
    )
