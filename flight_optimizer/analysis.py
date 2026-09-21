"""Everything needed to report or alert on one trip, computed once.

Search through the gateways, look up flight times for the leading candidates and
enforce the connection rules, rank against balances, and value against cash. Every trip
is analyzed once per cabin (economy, premium economy, business). The report and the
alerts both call `analyze_trip_cabins`, so they can never disagree.
"""

import sqlite3
from dataclasses import dataclass

from flight_optimizer import db
from flight_optimizer.affordability import TripOption, rank_trip
from flight_optimizer.cash_fares import SerpApiClient, SerpApiError
from flight_optimizer.enrichment import EnrichmentStats, enrich_result
from flight_optimizer.itineraries import EVALUATED_CABINS, TripSearchResult, search_trip_cabins
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
    cabin: Cabin = Cabin.BUSINESS

    @property
    def affordable(self) -> list[TripOption]:
        return [option for option in self.ranked if option.affordability.affordable]


def analyze_trip_cabins(
    conn: sqlite3.Connection,
    awards: SeatsAeroClient,
    cash: SerpApiClient | None,
    trip: sqlite3.Row,
    balances: dict[str, int],
    *,
    cabins: tuple[Cabin, ...] = EVALUATED_CABINS,
) -> list[TripAnalysis]:
    """One analysis per cabin, in `cabins` order. The award search is shared between cabins."""
    gateways = db.get_gateways(conn, trip["id"])
    found = search_trip_cabins(awards, trip, gateways, cabins=cabins)
    cash_broken: str | None = None  # once cash search fails, don't retry it for every cabin
    analyses = []
    for cabin in cabins:
        analysis, cash_broken = _analyze_cabin(
            conn, awards, cash, trip, balances, gateways, cabin, found[cabin], cash_broken
        )
        analyses.append(analysis)
    return analyses


def _analyze_cabin(
    conn, awards, cash, trip, balances, gateways, cabin: Cabin, found: TripSearchResult, cash_broken: str | None
) -> tuple[TripAnalysis, str | None]:
    verified, stats = enrich_result(awards, found, cabin=cabin, balances=balances)
    ranked = rank_trip(verified, balances)

    baseline, note = None, cash_broken
    if cash is None:
        note = "Cash prices skipped: SERPAPI_API_KEY is not set."
    elif cash_broken is None:
        try:
            baseline = trip_baseline(conn, cash, trip, cabin=cabin)
        except SerpApiError as exc:
            note = cash_broken = f"Cash prices unavailable: {exc}"

    analysis = TripAnalysis(
        trip=trip, gateways=gateways, ranked=ranked, baseline=baseline, cash_note=note,
        stats=stats, cabin=cabin,
    )
    return analysis, cash_broken
