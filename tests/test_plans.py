from datetime import datetime, timedelta

import pytest

from builders import JAN9, JAN16, RECENT, add_trip, analysis_for, baseline, good_option, leg, option
from flight_optimizer import db
from flight_optimizer.plans import latest_plan, render_plan, save_plan
from flight_optimizer.promos import Promo, PromoTier
from flight_optimizer.seats_aero import Cabin

GENERATED = datetime(2026, 9, 20, 14, 5)
BALANCES = {"capital_one": 115_519, "delta": 30_000}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def plan_for(conn, options=None, promos=None, **kwargs):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options or [good_option(seats=2)], **kwargs)
    return trip, render_plan(
        analysis, analysis.affordable[0], balances=BALANCES, generated_at=GENERATED, promos=promos
    )


def delta_sale(tiers=(PromoTier(2_000, None, 50),)):
    return {"delta": Promo(key="k", program="delta", kind="bonus", headline_percent=50, tiers=tiers,
                           ends=datetime(2026, 10, 1).date(), title="Delta sale", url="https://sale.example",
                           source="test", last_seen=GENERATED.date())}


def section(text):
    return text[text.index("MILES VS CASH"):text.index("NOTES")]


def row(text, label):
    """A table row's cells after the label, with the column padding collapsed."""
    line = next(line for line in section(text).splitlines() if line.strip().startswith(label))
    return " ".join(line.strip()[len(label):].split())


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

    assert "about 106.00 CAD (Seats.aero reports 10600, in hundredths)" in text
    assert "assumed hundredths" not in text        # CAD was checked against aircanada.com


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

    assert "THE OPTION: 71,800 miles + $305 taxes, 2.73 c/mile vs business cash $2,267 (0.55 vs economy cash $699)" in text
    assert "converted to dollars at fixed approximate rates" in text and "only CAD is confirmed" in text
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


def test_an_unchecked_currency_keeps_the_caution():
    from dataclasses import replace
    from flight_optimizer.plans import _taxes_line
    from builders import leg

    aud = replace(leg("RNO", "LAX", "qantas", 25_000, JAN9), total_taxes=1_410, currency="AUD")
    usd = replace(aud, currency="USD")

    assert "about 14.10 AUD (Seats.aero reports 1410; assumed hundredths, so confirm on the booking page)" == _taxes_line(aud).split("taxes/fees: ")[1]
    assert "assumed hundredths" in _taxes_line(usd)


def test_the_confirmed_currencies_say_where_they_were_checked():
    from flight_optimizer.seats_aero import TAX_UNIT_CONFIRMED

    assert set(TAX_UNIT_CONFIRMED) == {"CAD"} and "aircanada.com" in TAX_UNIT_CONFIRMED["CAD"]


def test_miles_vs_cash_puts_the_cash_value_and_buy_prices_per_mile_side_by_side(conn):
    _, text = plan_for(conn)

    table = section(text)
    assert "confirm on the program's site before paying" in table
    assert row(text, "cash, business") == "71,800 2.73 $2,267 what each mile replaces, after $305 taxes"
    assert row(text, "cash, economy").startswith("71,800 0.55 $699")
    assert row(text, "buy aeroplan") == "50,000 2.70 $1,350 low-confidence price"
    assert row(text, "or Capital One").startswith("50,000 1.00 $500 transfer 1:1")
    # 21,800 miles needed, sold in 2,000 blocks: priced per mile USED, not per mile bought
    assert row(text, "buy delta") == "21,800 3.80 $828 sold in blocks: buys 22,000"
    assert "Capital One" not in table.split("buy delta")[1]  # Delta is not a transfer partner


def test_the_verdict_is_on_the_whole_trip_not_one_program(conn):
    _, text = plan_for(conn)

    # aeroplan at 2.70 c is under the 2.73 c cash line, but buying everything still costs more
    assert "Buying every mile: $2,178 + $305 taxes = $2,483, $216 MORE than cash $2,267." in section(text)


def test_a_live_sale_prices_the_miles_and_shows_retail_beside_it(conn):
    _, text = plan_for(conn, promos=delta_sale())

    table = section(text)
    assert row(text, "buy delta") == "21,800 2.76 $602 50% bonus sale until Oct 01; retail 3.80 c/mile"
    assert "BEST CASE" not in table
    assert "$1,952 + $305 taxes = $2,257, $10 less than cash $2,267" in table


def test_a_sale_with_unknown_tiers_is_labelled_a_best_case(conn):
    _, text = plan_for(conn, promos=delta_sale(tiers=()))

    assert "BEST CASE: this sale's tiers are not known" in section(text)


def test_miles_that_cannot_be_bought_make_the_total_a_floor(conn):
    qantas = option(
        [leg("RNO", "LAX", "delta", 5_000, JAN9), leg("LAX", "SJO", "qantas", 20_000, JAN9)],
        [leg("SJO", "LAX", "qantas", 20_000, JAN16), leg("LAX", "RNO", "delta", 5_000, JAN16)],
    )

    _, text = plan_for(conn, [qantas])

    table = section(text)
    assert row(text, "buy qantas") == "40,000 - - cannot be bought"
    assert row(text, "or Capital One").startswith("40,000 1.00 $400")
    assert "Buying every mile: $376+ + $305 taxes = $682+ (some miles cannot be bought" in table


def test_a_purchase_over_the_yearly_limit_says_so(conn):
    balances = {"capital_one": 0, "delta": 100_000}
    big = option(
        [leg("RNO", "LAX", "delta", 35_000, JAN9), leg("LAX", "SJO", "delta", 1_000, JAN9)],
        [leg("SJO", "LAX", "delta", 35_000, JAN16), leg("LAX", "RNO", "delta", 1_000, JAN16)],
        balances=balances,
    )

    _, text = plan_for(conn, [big])

    assert row(text, "buy delta") == "72,000 - - over the 60,000 yearly purchase limit"


def test_without_a_cash_fare_the_buy_prices_are_still_shown(conn):
    _, text = plan_for(conn, baseline_value=None)

    table = section(text)
    assert "no cash fare for these dates" in table and "buy delta" in table
    assert "Buying every mile: $2,178 + $305 taxes = $2,483." in table


def test_fallback_options_carry_their_cents_per_mile_and_buy_everything_cost(conn):
    dearer = good_option(out_miles=15_000, flights=("UA7", "AV641", "AV654", "DL9"))

    _, text = plan_for(conn, [good_option(seats=2), dearer])

    assert "75,900 miles (+4,100), 2.58 c/mile vs cash, buy every mile $2,328 + $305 taxes = $2,634" in text


def test_the_columns_line_up_whatever_the_longest_label(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [good_option(seats=2)], cabin=Cabin.PREMIUM_ECONOMY)

    text = render_plan(analysis, analysis.affordable[0], balances=BALANCES, generated_at=GENERATED)

    lines = [line for line in section(text).splitlines()[1:] if not line.startswith("  Buying") and "c/mile" not in line
             and line.strip() and not line.startswith("  Paying") and not line.startswith("  by the total")]
    assert any(line.strip().startswith("cash, premium economy") for line in lines)
    header = section(text).splitlines()[1]
    column = header.index("c/mile") + len("c/mile")  # right edge of the cents column
    assert all(line[column - 4:column].replace(".", "").strip().isdigit() for line in lines)


def test_an_exact_trip_leaving_the_day_before_is_not_labelled_flexible(conn):
    early = option(
        [leg("RNO", "LAX", "delta", 5_000, JAN9 - timedelta(days=1),
             departs=datetime(2027, 1, 8, 18, 0), arrives=datetime(2027, 1, 8, 20, 0)),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, departs=datetime(2027, 1, 9, 0, 30), arrives=datetime(2027, 1, 9, 7, 0))],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 5_000, JAN16)],
    )

    _, text = plan_for(conn, [early])

    head = text.splitlines()[0]
    assert "Jan 09 2027 (leave Jan 08 2027) - Jan 16 2027" in head
    assert "you asked" not in head and "flexible" not in head
