from datetime import date, datetime, timezone

import pytest

from flight_optimizer import db
from flight_optimizer.__main__ import main
from flight_optimizer.affordability import TripOption, assess
from flight_optimizer.cash_fares import CashFare, CashSearch, SerpApiError
from flight_optimizer.itineraries import Itinerary
from flight_optimizer.report import build_report, render_trip
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import AwardResult, Cabin

JAN9, JAN10, JAN16 = date(2027, 1, 9), date(2027, 1, 10), date(2027, 1, 16)
TODAY = date(2026, 9, 20)
BALANCES = {"capital_one": 115_519, "american": 26_115, "delta": 14_049, "aeroplan": 0}


def leg(origin, dest, source, miles, on, *, direct=True, seats=2, updated="2026-09-19T00:00:00Z"):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS,
        miles=miles, total_taxes=0, currency="USD", remaining_seats=seats, airlines="XX",
        direct=direct, updated_at=updated,
    )


def option(out_legs, back_legs, balances=BALANCES):
    itineraries = (Itinerary(legs=tuple(out_legs), via="LAX"), Itinerary(legs=tuple(back_legs), via="LAX"))
    return TripOption(itineraries=itineraries, affordability=assess(itineraries, balances))


def best_option(**overrides):
    return option(
        [leg("RNO", "LAX", "american", 25_000, JAN9, direct=False), leg("LAX", "SJO", "aeroplan", 25_000, overrides.get("d2", JAN9))],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, direct=False), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )


def fare(price, total=700, layovers=(120,)):
    return CashFare(price=price, total_duration=total, layovers=layovers,
                    airlines=("United",), flight_numbers=("UA 1",), cabins=("x",))


BASELINE = TripBaseline(
    cabin_fares=(fare(1505, 795, (238, 85)), fare(762, 787, (148, 78))),
    economy_fares=(fare(323, 715, (212,)), fare(376, 867, (193, 118))),
)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def add_trip(conn, **overrides):
    fields = dict(name="Costa Rica", origin="RNO", destination="SJO",
                  depart_date=JAN9, return_date=JAN16, active=True)
    trip_id = db.add_trip(conn, **{**fields, **overrides})
    return conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()


def render(conn, ranked, baseline=BASELINE, **kwargs):
    trip = add_trip(conn)
    return render_trip(trip, ["DEN", "LAX"], ranked, baseline, BALANCES, today=TODAY, **kwargs)


# --- rendering ---------------------------------------------------------------

def test_header_and_baseline_section(conn):
    text = render(conn, [best_option()])

    assert "Costa Rica   RNO > SJO   Jan 09 2027 - Jan 16 2027   (business)" in text
    assert "Via gateways: DEN, LAX" in text
    assert "layovers <= 4h, total <= 15h" in text
    assert "TOTAL $2,267" in text and "TOTAL $699" in text
    assert "business costs $1,568 more than economy" in text


def test_option_shows_miles_value_legs_and_payment(conn):
    text = render(conn, [best_option()])

    assert "#1  85,900 miles   2.64 c/mile vs business cash   0.81 c/mile vs economy cash" in text
    assert "RNO>LAX Jan 09" in text and "american*" in text  # * marks a connecting leg
    assert "own balances: american 25,000, delta 10,900" in text
    assert "transfer from Capital One: 50,000 -> aeroplan (65,519 left)" in text
    assert "(1 of 1 combinations)" in text


def test_same_shape_date_variants_are_collapsed(conn):
    ranked = [best_option(d2=JAN9), best_option(d2=JAN10)]

    text = render(conn, ranked)

    assert text.count("#") == 1
    assert "(+1 other date combination at the same price)" in text


def test_warnings_flag_connections_same_day_zero_seats_and_stale_data(conn):
    risky = option(
        [leg("RNO", "LAX", "american", 25_000, JAN9, direct=False, updated="2026-05-12T00:00:00Z"),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, seats=0)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )

    text = render(conn, [risky])

    assert "connecting flights (*), check the routing: RNO>LAX american" in text
    assert "same-day connection at LAX on Jan 09" in text
    assert "0 seats reported: LAX>SJO aeroplan" in text
    assert "last refreshed: RNO>LAX american (2026-05-12)" in text
    # Only the legs at fault are named.
    assert "0 seats reported: RNO>LAX" not in text


def test_clean_option_has_no_check_line(conn):
    # Every connection is overnight, so no same-day warning applies.
    clean = option(
        [leg("RNO", "LAX", "delta", 10_900, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 3_000, date(2027, 1, 17))],
    )

    assert "check:" not in render(conn, [clean])


def test_closest_misses_shown_with_shortfall(conn):
    short = option(
        [leg("RNO", "LAX", "delta", 16_700, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "american", 19_000, date(2027, 1, 17))],
    )

    text = render(conn, [best_option(), short])

    assert "CLOSEST MISSES" in text
    assert "2,651 short in delta" in text


def test_nothing_affordable_says_so(conn):
    short = option(
        [leg("RNO", "LAX", "delta", 16_700, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, date(2027, 1, 17))],
    )

    text = render(conn, [short])

    assert "Nothing affordable with current balances" in text
    assert "#1" not in text


def test_no_results_and_no_baseline_still_render(conn):
    text = render(conn, [], baseline=None, cash_note="Cash prices skipped: SERPAPI_API_KEY is not set.")

    assert "No award options found through the gateways." in text
    assert "Cash prices skipped" in text


def test_top_n_limits_the_options_shown(conn):
    options = [
        option([leg("RNO", "LAX", "delta", 1_000 + i, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
               [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 2_000, date(2027, 1, 17))])
        for i in range(4)
    ]

    text = render(conn, options, top_n=2)

    assert "#2" in text and "#3" not in text


# --- build_report ------------------------------------------------------------

class FakeAwards:
    calls_remaining = 950

    def __init__(self, table=None):
        self.table = table or {}
        self.calls = []

    def search(self, origin, destination, start, end, **_):
        self.calls.append((origin, destination))
        return [r for r in self.table.get((origin, destination), []) if start <= r.date <= end]


class FakeCash:
    def __init__(self, error=None):
        self.error = error
        self.searches_made = 0

    def search(self, origin, destination, on_date, cabin, **_):
        self.searches_made += 1
        if self.error:
            raise self.error
        return CashSearch(fares=(fare(1000 if cabin is Cabin.BUSINESS else 300, 600, (60,)),),
                          lowest_price=None, typical_range=None,
                          fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


AWARD_TABLE = {
    ("RNO", "LAX"): [leg("RNO", "LAX", "delta", 10_900, JAN9)],
    ("LAX", "SJO"): [leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
    ("SJO", "LAX"): [leg("SJO", "LAX", "aeroplan", 25_000, JAN16)],
    ("LAX", "RNO"): [leg("LAX", "RNO", "delta", 10_900, JAN16)],
}


def test_no_active_trips_message(conn):
    add_trip(conn, active=False)

    assert build_report(conn, FakeAwards(), None).startswith("No active trips")


def test_report_covers_active_trips_only_and_shows_usage(conn):
    active = add_trip(conn, name="Active One")
    add_trip(conn, name="Shelved One", active=False)
    db.set_gateways(conn, active["id"], ["LAX"])
    db.set_balance(conn, "delta", 30_000)
    db.set_balance(conn, "capital_one", 100_000)
    awards, cash = FakeAwards(AWARD_TABLE), FakeCash()

    text = build_report(conn, awards, cash, today=TODAY)

    assert "Active One" in text and "Shelved One" not in text
    assert "#1  71,800 miles" in text
    assert "Seats.aero calls left today: 950" in text
    assert "SerpApi searches this run: 4" in text
    assert "Legs reporting 0 seats are excluded as unbookable" in text


def test_report_without_a_cash_client_says_so(conn):
    add_trip(conn)

    text = build_report(conn, FakeAwards(), None, today=TODAY)

    assert "Cash prices skipped: SERPAPI_API_KEY is not set." in text
    assert "SerpApi searches" not in text


def test_cash_failure_degrades_instead_of_crashing(conn):
    add_trip(conn)

    text = build_report(conn, FakeAwards(AWARD_TABLE), FakeCash(error=SerpApiError("quota gone")), today=TODAY)

    assert "Cash prices unavailable: quota gone" in text
    assert "CLOSEST MISSES" in text or "BEST AFFORDABLE OPTIONS" in text


# --- command line ------------------------------------------------------------

def test_cli_without_a_seats_key_reports_the_error(monkeypatch, capsys):
    monkeypatch.delenv("SEATS_AERO_API_KEY", raising=False)

    assert main([], env_file=None) == 1
    assert "SEATS_AERO_API_KEY" in capsys.readouterr().err


def test_cli_with_no_active_trips_makes_no_network_calls(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "k")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "empty.db"))

    assert main(["--top", "3"], env_file=None) == 0
    assert "No active trips" in capsys.readouterr().out
