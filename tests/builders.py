"""Shared builders for tests that need realistic options, trips and analyses."""

from datetime import date, datetime, timedelta

from flight_optimizer import db
from flight_optimizer.affordability import TripOption, assess
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.cash_fares import CashFare
from flight_optimizer.enrichment import EnrichmentStats
from flight_optimizer.itineraries import Itinerary
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import AwardResult, Cabin

JAN9, JAN10, JAN16 = date(2027, 1, 9), date(2027, 1, 10), date(2027, 1, 16)
# Award data dated yesterday: always "current", whenever the tests run.
RECENT = (date.today() - timedelta(days=1)).isoformat() + "T00:00:00Z"
BALANCES = {"capital_one": 115_519, "american": 26_115, "delta": 30_000, "aeroplan": 0}


def leg(origin, dest, source, miles, on, *, direct=True, seats=5, updated=RECENT,
        departs=None, arrives=None, flights="", url="", stops=None):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS,
        miles=miles, total_taxes=10_600, currency="CAD", remaining_seats=seats, airlines="XX",
        direct=direct, updated_at=updated, departs_at=departs, arrives_at=arrives,
        flight_numbers=flights, booking_url=url, stops=stops,
    )


def option(out_legs, back_legs, balances=BALANCES, via="LAX"):
    itineraries = (Itinerary(legs=tuple(out_legs), via=via), Itinerary(legs=tuple(back_legs), via=via))
    return TripOption(itineraries=itineraries, affordability=assess(itineraries, balances))


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute)


def good_option(*, seats=5, out_miles=10_900, flights=("UA1", "AV641", "AV654", "DL9"), updated=RECENT):
    """Round trip through LAX with 4-hour connections, two programs each way."""
    a, b, c, d = flights
    return option(
        [leg("RNO", "LAX", "delta", out_miles, JAN9, seats=seats, departs=at(JAN9, 7), arrives=at(JAN9, 10),
             flights=a, updated=updated, url="https://book.example/rno-lax"),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, seats=seats, departs=at(JAN9, 14), arrives=at(JAN9, 20),
             flights=b, updated=updated, url="https://book.example/lax-sjo")],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, seats=seats, departs=at(JAN16, 6), arrives=at(JAN16, 10),
             flights=c, updated=updated, url="https://book.example/sjo-lax"),
         leg("LAX", "RNO", "delta", 10_900, JAN16, seats=seats, departs=at(JAN16, 14), arrives=at(JAN16, 16),
             flights=d, updated=updated, url="https://book.example/lax-rno")],
    )


def fare(price, layovers=(200,), total=700):
    return CashFare(price=price, total_duration=total, layovers=layovers, airlines=("United",),
                    flight_numbers=("UA 1",), cabins=("x",))


def baseline():
    return TripBaseline(cabin_fares=(fare(1505), fare(762)), economy_fares=(fare(323), fare(376)))


def add_trip(conn, **overrides):
    fields = dict(name="Costa Rica", origin="RNO", destination="SJO",
                  depart_date=JAN9, return_date=JAN16, active=True)
    trip_id = db.add_trip(conn, **{**fields, **overrides})
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


def analysis_for(trip, ranked, *, baseline_value="default", cabin=Cabin.BUSINESS):
    return TripAnalysis(
        trip=trip, gateways=["LAX"], ranked=ranked,
        baseline=baseline() if baseline_value == "default" else baseline_value,
        cash_note=None, stats=EnrichmentStats(), cabin=cabin,
    )
