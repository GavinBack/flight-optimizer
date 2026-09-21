from datetime import datetime

import pytest

from builders import JAN9, JAN16, RECENT, add_trip, analysis_for, baseline, good_option, leg, option
from flight_optimizer import db
from flight_optimizer.plans import latest_plan, render_plan, save_plan

GENERATED = datetime(2026, 9, 20, 14, 5)
BALANCES = {"capital_one": 115_519, "delta": 30_000}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def plan_for(conn, options=None, **kwargs):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options or [good_option(seats=2)], **kwargs)
    return trip, render_plan(analysis, analysis.affordable[0], balances=BALANCES, generated_at=GENERATED)


def test_steps_come_in_the_safe_order_verify_then_transfer_then_book(conn):
    _, text = plan_for(conn)

    verify, transfer, book, after = (
        text.index("STEP 1. VERIFY"), text.index("STEP 2. TRANSFER"),
        text.index("STEP 3. BOOK"), text.index("STEP 4. AFTER BOOKING"),
    )
    assert verify < transfer < book < after
    assert "BEFORE moving any miles" in text and "cannot be undone" in text


def test_every_leg_is_listed_with_times_flights_seats_link_and_data_age(conn):
    _, text = plan_for(conn)

    assert "OUT  RNO>LAX  Sat Jan 09 07:00-10:00 (local)  flights UA1" in text
    assert "delta: 10,900 miles; 2 seats" in text
    assert "open: https://book.example/lax-sjo" in text
    assert f"data last refreshed {RECENT[:10]}" in text
    assert "connection at LAX: 4h00m" in text


def test_the_plan_warns_that_booking_links_may_open_in_the_wrong_cabin(conn):
    _, text = plan_for(conn)

    assert "The links pre-fill route and date, not always the cabin" in text and "set the cabin yourself" in text


def test_taxes_are_shown_with_the_unit_assumption_stated(conn):
    _, text = plan_for(conn)

    assert "about 106.00 CAD" in text and "assumed hundredths" in text


def test_the_transfer_step_gives_amounts_and_what_is_left(conn):
    _, text = plan_for(conn)

    assert "Move 50,000 Capital One miles to aeroplan" in text
    assert "Capital One miles left afterwards: 65,519" in text


def test_no_transfer_step_content_when_every_leg_is_paid_from_own_balances(conn):
    own = option(
        [leg("RNO", "LAX", "delta", 5_000, JAN9), leg("LAX", "SJO", "delta", 5_000, JAN9)],
        [leg("SJO", "LAX", "delta", 5_000, JAN16), leg("LAX", "RNO", "delta", 5_000, JAN16)],
    )

    _, text = plan_for(conn, [own])

    assert "No transfer needed" in text and "Move " not in text


def test_the_scarcest_leg_is_booked_first(conn):
    mixed = option(
        [leg("RNO", "LAX", "delta", 10_900, JAN9, seats=9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, seats=1)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, seats=4), leg("LAX", "RNO", "delta", 10_900, JAN16, seats=7)],
    )

    _, text = plan_for(conn, [mixed])
    book = text[text.index("STEP 3. BOOK"):text.index("STEP 4.")]

    numbered = [line.strip() for line in book.splitlines() if line.strip()[:2] in ("1.", "2.", "3.", "4.")]
    assert numbered[0].startswith("1. LAX>SJO") and "(1 seat left)" in numbered[0]
    assert numbered[1].startswith("2. SJO>LAX")  # 4 seats
    assert numbered[-1].startswith("4. RNO>LAX")  # 9 seats


def test_the_done_command_for_this_trip_is_included(conn):
    trip, text = plan_for(conn)

    assert f"python -m flight_optimizer done {trip['id']}" in text


def test_urgency_and_snapshot_warnings_are_up_top(conn):
    _, text = plan_for(conn)

    head = text.split("\n\n")[0]
    assert "Urgency: ACT TODAY" in head and "a guide, not a guarantee" in head
    assert "snapshot: confirm each flight is still bookable" in head
    assert "Generated 2026-09-20 14:05" in head


def test_headline_shows_value_against_cash(conn):
    _, text = plan_for(conn)

    assert "THE OPTION: 71,800 miles, 3.16 c/mile vs business cash $2,267 (0.97 vs economy cash $699)" in text
    assert "4 bookings." in text


def test_fallback_options_are_listed_when_they_exist(conn):
    dearer = good_option(out_miles=15_000, flights=("UA7", "AV641", "AV654", "DL9"))

    _, text = plan_for(conn, [good_option(seats=2), dearer])

    fallback = text[text.index("IF THIS OPTION DISAPPEARS"):]
    assert "75,900 miles (+4,100)" in fallback and "UA7" in fallback


def test_says_so_when_there_is_no_fallback(conn):
    _, text = plan_for(conn)

    assert "No other affordable option right now." in text


def test_the_cash_alternative_is_included_when_a_fare_exists(conn):
    _, text = plan_for(conn)
    assert "CASH ALTERNATIVE" in text and "$2,267 round trip" in text and "economy: $699" in text

    _, without = plan_for(add_conn(), baseline_value=None)
    assert "CASH ALTERNATIVE" not in without


def add_conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    return connection


def test_save_plan_writes_a_named_file_and_latest_plan_finds_the_newest(conn, tmp_path):
    trip, text = plan_for(conn)

    first = save_plan(text, trip, generated_at=datetime(2026, 9, 20, 14, 5), directory=tmp_path / "plans")
    second = save_plan(text + "\nnewer", trip, generated_at=datetime(2026, 9, 21, 9, 30), directory=tmp_path / "plans")

    assert first.name == "1-costa-rica-business-20260920-1405.txt"
    assert first.read_text().startswith("BOOKING PLAN")
    assert latest_plan(trip["id"], tmp_path / "plans") == second


def test_plans_are_kept_per_cabin_and_the_newest_wins_whatever_its_cabin(conn, tmp_path):
    from flight_optimizer.seats_aero import Cabin

    trip, text = plan_for(conn)
    directory = tmp_path / "plans"
    business = save_plan(text, trip, generated_at=datetime(2026, 9, 20, 14, 5), cabin=Cabin.BUSINESS, directory=directory)
    economy = save_plan(text, trip, generated_at=datetime(2026, 9, 21, 9, 30), cabin=Cabin.ECONOMY, directory=directory)

    assert latest_plan(trip["id"], directory) == economy      # newest overall, though "economy" sorts first
    assert latest_plan(trip["id"], directory, Cabin.BUSINESS) == business
    assert latest_plan(trip["id"], directory, Cabin.PREMIUM_ECONOMY) is None


def test_latest_plan_is_none_when_nothing_was_saved(tmp_path):
    assert latest_plan(1, tmp_path / "nope") is None
    (tmp_path / "plans").mkdir()
    assert latest_plan(1, tmp_path / "plans") is None


def test_latest_plan_does_not_confuse_trip_1_with_trip_10(tmp_path):
    directory = tmp_path / "plans"
    directory.mkdir()
    (directory / "10-other-20260920-1405.txt").write_text("ten")

    assert latest_plan(1, directory) is None
    assert latest_plan(10, directory).read_text() == "ten"


def qantas_option():
    return option(
        [leg("RNO", "LAX", "qantas", 29_000, JAN9, seats=2), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, seats=5)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, seats=5), leg("LAX", "RNO", "delta", 10_900, JAN16, seats=5)],
    )


def test_each_transfer_states_the_ratio_points_received_and_how_long_it_takes(conn):
    _, text = plan_for(conn, [qantas_option()])

    assert "Move 29,000 Capital One miles to qantas (1:1: you receive 29,000 points). Takes: up to 36 hours." in text
    assert "Move 50,000 Capital One miles to aeroplan (1:1: you receive 50,000 points). Takes: almost instant." in text


def test_a_slow_transfer_gets_an_explicit_warning_that_seats_may_not_last(conn):
    _, text = plan_for(conn, [qantas_option()])

    assert "WARNING: the qantas transfer takes up to 36 hours" in text and "seats may not last" in text
    assert "WARNING: the aeroplan" not in text  # instant ones don't warn


def test_no_slow_transfer_warning_when_every_transfer_is_near_instant(conn):
    _, text = plan_for(conn)

    assert "WARNING" not in text


def test_the_plan_no_longer_claims_transfers_are_usually_instant(conn):
    _, text = plan_for(conn, [qantas_option()])

    assert "usually instant" not in text


def virgin_option():
    return option(
        [leg("RNO", "LAX", "virginatlantic", 21_000, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )


def test_the_virgin_transfer_step_carries_the_account_guidance(conn):
    _, text = plan_for(conn, [virgin_option()])

    assert "to virginatlantic (1:1: you receive 21,000 points). Takes: about 1 business day." in text
    assert "sends this to your Virgin Red membership number" in text
    assert "free Flying Club account" in text and "Link accounts > Link Virgin Atlantic" in text


def test_only_virgin_gets_that_note(conn):
    _, text = plan_for(conn)

    assert "Virgin Red" not in text


def test_the_plan_lists_the_accounts_needed_and_how_each_is_funded(conn):
    _, text = plan_for(conn, [virgin_option()])

    section = text[text.index("BEFORE YOU START"):text.index("STEP 1.")]
    assert "Virgin Atlantic Flying Club (receives a Capital One transfer) plus a linked Virgin Red account" in section
    assert "Air Canada Aeroplan (receives a Capital One transfer)" in section
    assert "Delta SkyMiles (paid from your own balance)" in section


def test_each_program_is_listed_once_even_when_it_has_two_legs(conn):
    _, text = plan_for(conn)

    section = text[text.index("BEFORE YOU START"):text.index("STEP 1.")]
    assert section.count("Delta SkyMiles") == 1 and section.count("Air Canada Aeroplan") == 1


def test_accounts_come_before_the_verify_step(conn):
    _, text = plan_for(conn)

    assert text.index("BEFORE YOU START") < text.index("STEP 1. VERIFY")


def test_the_plan_header_states_data_freshness_relative_to_when_it_was_generated(conn):
    fresh = good_option(seats=2, updated="2026-09-19T00:00:00Z")
    old = good_option(seats=2, updated="2026-08-01T00:00:00Z")

    _, current = plan_for(conn, [fresh])
    _, stale = plan_for(add_conn(), [old])

    assert "Data freshness: data current, oldest 1d." in current.split("\n\n")[0]
    assert "Data freshness: DATA STALE, oldest 50d." in stale.split("\n\n")[0]


def test_a_plan_for_shifted_dates_shows_the_dates_to_book_and_what_was_asked(conn):
    from datetime import date
    from builders import leg, option

    trip = add_trip(conn, flexible=True)
    shifted = option(
        [leg("RNO", "LAX", "delta", 10_900, date(2027, 1, 7)), leg("LAX", "SJO", "aeroplan", 25_000, date(2027, 1, 7))],
        [leg("SJO", "LAX", "aeroplan", 25_000, date(2027, 1, 14)), leg("LAX", "RNO", "delta", 10_900, date(2027, 1, 14))],
    )
    analysis = analysis_for(trip, [shifted])

    text = render_plan(analysis, shifted, balances=BALANCES, generated_at=GENERATED)

    assert "Jan 07 2027 - Jan 14 2027 (you asked Jan 09 2027 - Jan 16 2027, flexible)" in text.splitlines()[0]


def test_a_plan_on_the_asked_dates_is_headed_as_before(conn):
    _, text = plan_for(conn)

    assert "Jan 09 2027 - Jan 16 2027   (business)" in text.splitlines()[0] and "you asked" not in text


def test_a_plan_lists_what_it_leaves_the_other_trips(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [good_option(seats=2)])
    note = "Shared miles: with this booked, Kelowna keeps only economy by award."

    text = render_plan(analysis, analysis.affordable[0], balances=BALANCES, generated_at=GENERATED, notes=(note,))

    assert "OTHER TRIPS SHARE THESE MILES" in text and f"  {note}" in text
    assert text.index("OTHER TRIPS SHARE") < text.index("\nNOTES")


def test_a_plan_without_notes_has_no_such_section(conn):
    _, text = plan_for(conn)

    assert "OTHER TRIPS" not in text
