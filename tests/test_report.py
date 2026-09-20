from datetime import date, datetime, timedelta, timezone

import pytest

from flight_optimizer import db
from flight_optimizer.__main__ import main
from flight_optimizer.affordability import TripOption, assess
from flight_optimizer.cash_fares import CashFare, CashSearch, SerpApiError
from flight_optimizer.itineraries import Itinerary
from flight_optimizer.report import build_report, render_trip
from flight_optimizer.savings import TripBaseline
from flight_optimizer.enrichment import EnrichmentStats
from flight_optimizer.seats_aero import AwardResult, Cabin, FlightOption, TripDetails

JAN9, JAN10, JAN16 = date(2027, 1, 9), date(2027, 1, 10), date(2027, 1, 16)
TODAY = date(2026, 9, 20)
BALANCES = {"capital_one": 115_519, "american": 26_115, "delta": 14_049, "aeroplan": 0}


def leg(origin, dest, source, miles, on, *, direct=True, seats=2, updated="2026-09-19T00:00:00Z", aid="", departs=None, arrives=None):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS,
        miles=miles, total_taxes=0, currency="USD", remaining_seats=seats, airlines="XX",
        direct=direct, updated_at=updated, availability_id=aid, departs_at=departs, arrives_at=arrives,
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
    assert "layovers over 1h and up to 4h, total <= 15h" in text
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

    assert sum(1 for line in text.splitlines() if line.startswith("#")) == 1
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

    assert "check:" not in render(conn, [clean], baseline=None)


def one_ticket_option():
    return option(
        [leg("RNO", "LAX", "aeroplan", 12_500, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "aeroplan", 12_500, JAN16)],
    )


def self_transfer_option():
    return option(
        [leg("RNO", "LAX", "american", 10_000, JAN9), leg("LAX", "SJO", "aeroplan", 15_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 15_000, JAN16), leg("LAX", "RNO", "delta", 10_000, JAN16)],
    )


def test_tickets_line_distinguishes_one_ticket_from_self_transfers(conn):
    text = render(conn, [one_ticket_option(), self_transfer_option()])

    assert "tickets: out 1 booking (aeroplan), connecting at LAX; back 1 booking (aeroplan), connecting at LAX" in text
    assert "out 2 bookings, self-transfer at LAX (american + aeroplan)" in text
    assert text.count("bags re-checked, no missed-connection cover there") == 1  # only on the mixed option


def test_overnight_stop_in_one_program_is_reported_as_two_bookings(conn):
    overnight = option(
        [leg("RNO", "LAX", "aeroplan", 12_500, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "aeroplan", 12_500, JAN16)],
    )

    assert "self-transfer at LAX (aeroplan, overnight stop)" in render(conn, [overnight])


def test_preference_note_states_what_single_ticket_costs(conn):
    text = render(conn, [one_ticket_option(), self_transfer_option()])

    assert "Ranked single-ticket first: #1 costs 25,000 more miles than the cheapest affordable option (50,000 miles, 2 self-transfers)." in text


def test_no_preference_note_when_the_single_ticket_option_is_also_cheapest(conn):
    assert "Ranked single-ticket first" not in render(conn, [one_ticket_option()])


def test_report_says_when_no_single_ticket_option_is_affordable(conn):
    text = render(conn, [self_transfer_option()])

    assert "No fully single-ticket option is affordable" in text


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

    def __init__(self, table=None, details=None):
        self.table = table or {}
        self.details = details or {}
        self.calls = []

    def trips(self, availability_id):
        return self.details[availability_id]

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
        return CashSearch(fares=(fare(1000 if cabin is Cabin.BUSINESS else 300, 600, (120,)),),
                          lowest_price=None, typical_range=None,
                          fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


AWARD_TABLE = {
    ("RNO", "LAX"): [leg("RNO", "LAX", "delta", 10_900, JAN9, aid="RL")],
    ("LAX", "SJO"): [leg("LAX", "SJO", "aeroplan", 25_000, JAN9, aid="LS")],
    ("SJO", "LAX"): [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, aid="SL")],
    ("LAX", "RNO"): [leg("LAX", "RNO", "delta", 10_900, JAN16, aid="LR")],
}


def flight_details(miles, departs, arrives, url="https://book.example/x"):
    option = FlightOption(
        cabin=Cabin.BUSINESS, miles=miles, seats=2, departs_at=departs, arrives_at=arrives,
        flight_numbers="XX100", stops=0, total_duration=180, total_taxes=0, currency="USD",
        updated_at="2026-09-19T00:00:00Z",
    )
    return TripDetails(options=(option,), booking_url=url, booking_links=(("Book", url),))


def connecting_details(day):
    """Lands at LAX at 10:00 and the onward flight leaves at 14:00: a comfortable 4 hours."""
    at = lambda h: datetime(day.year, day.month, day.day, h)
    return {
        "RL": flight_details(10_900, at(7), at(10)), "LS": flight_details(25_000, at(14), at(20)),
        "SL": flight_details(25_000, datetime(2027, 1, 16, 7), datetime(2027, 1, 16, 10)),
        "LR": flight_details(10_900, datetime(2027, 1, 16, 14), datetime(2027, 1, 16, 16)),
    }


ALL_DETAILS = {**connecting_details(JAN9), "SL": connecting_details(JAN16)["SL"], "LR": connecting_details(JAN16)["LR"]}


def test_no_active_trips_message(conn):
    add_trip(conn, active=False)

    assert build_report(conn, FakeAwards(), None).startswith("No active trips")


def test_report_covers_active_trips_only_and_shows_usage(conn):
    active = add_trip(conn, name="Active One")
    add_trip(conn, name="Shelved One", active=False)
    db.set_gateways(conn, active["id"], ["LAX"])
    db.set_balance(conn, "delta", 30_000)
    db.set_balance(conn, "capital_one", 100_000)
    awards, cash = FakeAwards(AWARD_TABLE, ALL_DETAILS), FakeCash()

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

    text = build_report(conn, FakeAwards(AWARD_TABLE, ALL_DETAILS), FakeCash(error=SerpApiError("quota gone")), today=TODAY)

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


# --- flight times, connection warnings, stats, cash through-ticket ------------------

def timed_option(connect_minutes, *, same_day=True):
    """Outbound lands at LAX at 10:00; the onward flight leaves `connect_minutes` later."""
    land = datetime(2027, 1, 9, 10, 0)
    out = [
        leg("RNO", "LAX", "delta", 10_900, JAN9, departs=land - timedelta(hours=2), arrives=land, aid="a"),
        leg("LAX", "SJO", "delta", 25_000, JAN9 if same_day else JAN10,
            departs=land + timedelta(minutes=connect_minutes), arrives=land + timedelta(minutes=connect_minutes, hours=5)),
    ]
    back_land = datetime(2027, 1, 16, 10, 0)
    back = [
        leg("SJO", "LAX", "delta", 25_000, JAN16, departs=back_land - timedelta(hours=4), arrives=back_land),
        leg("LAX", "RNO", "delta", 10_900, JAN16, departs=back_land + timedelta(hours=4), arrives=back_land + timedelta(hours=6)),
    ]
    return option(out, back, {"delta": 500_000, "capital_one": 0})


def test_legs_show_local_times_flight_numbers_and_the_connection_buffer(conn):
    o = timed_option(200)
    text = render(conn, [o])

    assert "RNO>LAX Jan 09 08:00-10:00" in text
    assert "LAX>SJO Jan 09 13:20-18:20" in text
    assert "[3h20m at LAX]" in text and "[4h00m at LAX]" in text


def test_a_tight_connection_warns_with_the_actual_time(conn):
    text = render(conn, [timed_option(95)])

    assert "tight connection at LAX 1h35m (over 1 hour but under 2.5 hours between flights)" in text


def test_a_comfortable_connection_does_not_warn(conn):
    text = render(conn, [timed_option(150)], baseline=None)

    assert "tight connection" not in text and "flight times not checked" not in text


def test_an_overnight_stop_is_called_out(conn):
    o = timed_option(24 * 60, same_day=False)

    assert "overnight stop at LAX (24h00m)" in render(conn, [o], baseline=None)


def test_untimed_same_day_options_still_say_times_were_not_checked(conn):
    assert "flight times not checked" in render(conn, [best_option()])


def test_stats_line_reports_lookups_and_what_was_dropped(conn):
    stats = EnrichmentStats(lookups=14, dropped_unsafe=3, unverifiable=2, budget_reached=True)

    text = render(conn, [best_option()], stats=stats)

    assert "Flight times checked for the leading options (14 lookups)" in text
    assert "3 dropped for a connection of 1 hour or less" in text
    assert "2 skipped for lack of usable flight details" in text
    assert "stopped early to protect the API quota" in text


def test_no_stats_line_when_nothing_was_looked_up(conn):
    assert "Flight times checked" not in render(conn, [best_option()], stats=EnrichmentStats())


def test_cash_through_ticket_is_listed_next_to_the_awards(conn):
    text = render(conn, [best_option()])

    assert "CASH THROUGH-TICKET" in text and "one booking each way" in text
    assert text.index("BEST AFFORDABLE OPTIONS") < text.index("CASH THROUGH-TICKET")
    assert "vs award #1: 85,900 miles on 4 separate bookings" in text


def test_cash_through_ticket_warns_about_short_layovers(conn):
    text = render(conn, [best_option()])

    assert "layover under 2.5 hours: out 1h25m, ret 2h28m, ret 1h18m" in text


def test_cash_through_ticket_is_omitted_when_no_fare_exists(conn):
    no_fare = TripBaseline(cabin_fares=(None, None), economy_fares=(None, None))

    assert "CASH THROUGH-TICKET" not in render(conn, [best_option()], baseline=no_fare)
    assert "CASH THROUGH-TICKET" not in render(conn, [best_option()], baseline=None)


def test_cash_ticket_is_shown_even_when_no_award_is_affordable(conn):
    text = render(conn, [])

    assert "CASH THROUGH-TICKET" in text and "vs award #1" not in text


def test_build_report_verifies_flight_times_and_shows_the_booking_data(conn):
    add_trip(conn)
    db.set_gateways(conn, 1, ["LAX"])
    db.set_balance(conn, "delta", 30_000)
    db.set_balance(conn, "capital_one", 100_000)

    text = build_report(conn, FakeAwards(AWARD_TABLE, ALL_DETAILS), FakeCash(), today=TODAY)

    assert "RNO>LAX Jan 09 07:00-10:00" in text and "[4h00m at LAX]" in text
    assert "Flight times checked" in text


def test_build_report_drops_options_whose_connection_is_an_hour_or_less(conn):
    add_trip(conn)
    db.set_gateways(conn, 1, ["LAX"])
    db.set_balance(conn, "delta", 30_000)
    db.set_balance(conn, "capital_one", 100_000)
    details = dict(ALL_DETAILS)
    at = lambda h, m=0: datetime(2027, 1, 9, h, m)
    details["LS"] = flight_details(25_000, at(10, 45), at(16))  # 45 minutes after landing

    text = build_report(conn, FakeAwards(AWARD_TABLE, details), None, today=TODAY)

    assert "0 of 0 combinations" in text
    assert "1 dropped for a connection of 1 hour or less" in text
