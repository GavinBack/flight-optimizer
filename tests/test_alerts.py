from datetime import date, datetime, timedelta, timezone

import pytest

from builders import (
    JAN9, JAN10, JAN16, RECENT, add_trip, analysis_for, at, baseline, good_option, leg, option,
)
from flight_optimizer import db
from flight_optimizer.alerts import (
    RE_ALERT_DAYS, assess_urgency, build_alert, decide_alert, decide_upgrade_alert, option_key, record_sent,
    record_upgrade_sent, usable_for_alert,
)
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import Cabin
from builders import fare


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("seats, label, priority", [
    (1, "ACT TODAY", 4), (2, "ACT TODAY", 4),
    (3, "ACT WITHIN A FEW DAYS", 3), (5, "ACT WITHIN A FEW DAYS", 3),
    (6, "NO RUSH", 2), (9, "NO RUSH", 2),
])
def test_urgency_follows_the_scarcest_leg(seats, label, priority):
    urgency = assess_urgency(good_option(seats=seats))

    assert (urgency.label, urgency.priority) == (label, priority)


def test_urgency_names_the_leg_that_is_running_out():
    scarce = option(
        [leg("RNO", "LAX", "delta", 10_900, JAN9, seats=9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, seats=1)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, seats=7), leg("LAX", "RNO", "delta", 10_900, JAN16, seats=8)],
    )

    urgency = assess_urgency(scarce)

    assert urgency.reason == "LAX>SJO aeroplan has 1 seat left"


def test_option_key_identifies_flights_not_prices():
    same_flights_new_price = good_option(out_miles=12_000)

    assert option_key(good_option()) == option_key(same_flights_new_price)
    assert option_key(good_option()) != option_key(good_option(flights=("UA2", "AV641", "AV654", "DL9")))


def test_option_key_separates_cabins_so_one_alert_does_not_silence_another():
    from dataclasses import replace
    from flight_optimizer.itineraries import Itinerary
    from flight_optimizer.affordability import TripOption
    from flight_optimizer.seats_aero import Cabin

    business = good_option()
    economy = TripOption(
        itineraries=tuple(
            Itinerary(legs=tuple(replace(l, cabin=Cabin.ECONOMY) for l in it.legs), via=it.via)
            for it in business.itineraries
        ),
        affordability=business.affordability,
    )

    assert option_key(business) != option_key(economy)


def alert_for(conn, options, **kwargs):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options, **kwargs)
    return trip, analysis, decide_alert(conn, analysis)


def test_the_message_is_a_short_summary_with_urgency_and_a_pointer_to_the_plan(conn):
    trip, analysis, alert = alert_for(conn, [good_option(seats=1)])

    lines = alert.message.split("\n")
    assert lines[0] == "RNO > SJO  Jan 09 - Jan 16  (business)"
    assert lines[1] == "71,800 miles + $305 taxes (2.7 c/mile vs $2,267 business cash)"
    assert lines[2] == "ACT TODAY: RNO>LAX delta has 1 seat left"
    assert "Full steps: python -m flight_optimizer plan 1" in lines[-1]
    assert alert.title == "Costa Rica (business): 71,800 miles"
    assert len(alert.message) < 600


def test_the_message_never_contains_balances(conn):
    _, _, alert = alert_for(conn, [good_option()])

    for secret in ("115,519", "115519", "30,000", "balance", "capital"):
        assert secret not in alert.message.lower()


def test_high_urgency_gets_high_priority_and_a_warning_tag(conn):
    _, _, urgent = alert_for(conn, [good_option(seats=1)])
    assert urgent.priority == 4 and urgent.tags == ("airplane", "warning")

    _, _, calm = alert_for(conn, [good_option(seats=9)])
    assert calm.priority == 2 and calm.tags == ("airplane",)


def test_tapping_the_notification_opens_the_first_booking_link(conn):
    _, _, alert = alert_for(conn, [good_option()])

    assert alert.click == "https://book.example/rno-lax"


def test_heads_up_notes_cover_separate_bookings_tight_connections_and_overnights(conn):
    tight = option(
        [leg("RNO", "LAX", "delta", 10_900, JAN9, departs=at(JAN9, 7), arrives=at(JAN9, 10)),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, departs=at(JAN9, 11, 30), arrives=at(JAN9, 17))],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, departs=at(JAN16, 12), arrives=at(JAN16, 22)),
         leg("LAX", "RNO", "delta", 10_900, JAN16 + timedelta(days=1), departs=at(JAN16 + timedelta(days=1), 9),
             arrives=at(JAN16 + timedelta(days=1), 11))],
    )

    _, _, alert = alert_for(conn, [tight])

    note = next(line for line in alert.message.split("\n") if line.startswith("Note:"))
    assert "tight 1h30m connection at LAX" in note
    assert "overnight at LAX" in note
    assert "separate bookings: bags re-checked, no missed-connection cover" in note


def test_a_fallback_line_shows_the_next_best_distinct_option(conn):
    dearer = good_option(out_miles=15_000, flights=("UA7", "AV641", "AV654", "DL9"))

    _, _, alert = alert_for(conn, [good_option(), dearer])

    assert "If it goes: next best is 75,900 miles (+4,100), changes RNO>LAX delta" in alert.message


def test_a_same_price_fallback_says_which_leg_changes(conn):
    other_flight = good_option(flights=("UA1", "AV999", "AV654", "DL9"))  # only LAX>SJO differs

    _, _, alert = alert_for(conn, [good_option(), other_flight])

    assert "If it goes: next best is 71,800 miles (+0), changes LAX>SJO aeroplan" in alert.message


def test_with_no_other_option_the_message_says_so(conn):
    _, _, alert = alert_for(conn, [good_option()])

    assert "If it goes: no other affordable option right now" in alert.message


def test_the_data_age_is_stated(conn):
    trip = add_trip(conn)
    stale = good_option(updated="2026-08-16T00:00:00Z")

    alert = build_alert(analysis_for(trip, [stale]), stale, fallback=None)  # bypasses the freshness gate

    assert "Data as of 2026-08-16." in alert.message


def test_without_a_cash_baseline_the_value_line_is_just_miles(conn):
    _, _, alert = alert_for(conn, [good_option()], baseline_value=None)

    assert alert.message.split("\n")[1] == "71,800 miles + $305 taxes"


def test_no_alert_when_nothing_is_affordable(conn):
    unaffordable = option(
        [leg("RNO", "LAX", "delta", 90_000, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )

    _, _, alert = alert_for(conn, [unaffordable])

    assert alert is None


def test_an_alert_is_sent_once_then_suppressed_within_the_window(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    assert first is not None
    record_sent(conn, first)

    assert decide_alert(conn, analysis) is None


def test_the_same_option_alerts_again_after_the_window(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    record_sent(conn, first)
    later = datetime.now(timezone.utc) + timedelta(days=RE_ALERT_DAYS + 1)
    refreshed = good_option(updated=later.date().isoformat() + "T00:00:00Z")  # Seats.aero refreshed it meanwhile

    assert decide_alert(conn, analysis_for(trip, [refreshed]), now=later) is not None


def test_a_cheaper_price_for_the_same_option_alerts_immediately(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    record_sent(conn, first)
    cheaper = analysis_for(trip, [good_option(out_miles=8_000)])

    again = decide_alert(conn, cheaper)

    assert again is not None and again.option.total_miles == 68_900


def test_a_dearer_price_for_the_same_option_stays_quiet(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    record_sent(conn, first)

    assert decide_alert(conn, analysis_for(trip, [good_option(out_miles=12_000)])) is None


def test_a_different_best_option_alerts_even_inside_the_window(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    record_sent(conn, first)
    different = analysis_for(trip, [good_option(flights=("UA2", "AV641", "AV654", "DL9"))])

    assert decide_alert(conn, different) is not None


def test_naive_local_now_is_accepted(conn):
    trip, analysis, first = alert_for(conn, [good_option()])
    record_sent(conn, first)

    assert decide_alert(conn, analysis, now=datetime.now()) is None  # a naive datetime must not raise


def test_record_sent_stores_the_key_miles_and_plan_path(conn):
    trip, _, alert = alert_for(conn, [good_option()])

    record_sent(conn, alert, "plans/1-costa-rica.txt")

    row = db.last_alert(conn, trip["id"], alert.key)
    assert row["miles"] == 71_800 and row["details"] == "plans/1-costa-rica.txt"
    assert row["alert_type"] == "award"


def test_a_slow_transfer_is_flagged_in_the_alert_note(conn):
    slow = option(
        [leg("RNO", "LAX", "qantas", 29_000, JAN9, departs=at(JAN9, 7), arrives=at(JAN9, 10)),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, departs=at(JAN9, 14), arrives=at(JAN9, 20))],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, departs=at(JAN16, 6), arrives=at(JAN16, 10)),
         leg("LAX", "RNO", "delta", 10_900, JAN16, departs=at(JAN16, 14), arrives=at(JAN16, 16))],
    )

    _, _, alert = alert_for(conn, [slow])

    note = next(line for line in alert.message.split("\n") if line.startswith("Note:"))
    assert "qantas transfer takes up to 36 hours" in note and "aeroplan transfer" not in note


def test_instant_transfers_add_nothing_to_the_alert(conn):
    _, _, alert = alert_for(conn, [good_option()])

    assert "transfer takes" not in alert.message


def hub_option(*, out_stops=0, back_stops=0, updated=RECENT):
    return option(
        [leg("RNO", "LAX", "delta", 10_900, JAN9, departs=at(JAN9, 7), arrives=at(JAN9, 10), stops=out_stops, updated=updated),
         leg("LAX", "SJO", "aeroplan", 25_000, JAN9, departs=at(JAN9, 14), arrives=at(JAN9, 20), stops=0, updated=updated)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, departs=at(JAN16, 6), arrives=at(JAN16, 10), stops=0, updated=updated),
         leg("LAX", "RNO", "delta", 10_900, JAN16, departs=at(JAN16, 14), arrives=at(JAN16, 16), stops=back_stops, updated=updated)],
    )


def note_of(alert):
    return next((line for line in alert.message.split("\n") if line.startswith("Note:")), "")


def test_a_hub_leg_of_several_flights_is_called_out(conn):
    _, _, alert = alert_for(conn, [hub_option(back_stops=2)])

    assert "LAX>RNO delta is 3 flights" in note_of(alert)


def test_a_hub_leg_with_a_single_stop_is_not_called_out(conn):
    _, _, alert = alert_for(conn, [hub_option(out_stops=1, back_stops=1)])

    assert "flights" not in note_of(alert)


def test_stale_data_is_named_in_the_note(conn):
    trip = add_trip(conn)
    stale = hub_option(updated="2020-01-01T00:00:00Z")

    alert = build_alert(analysis_for(trip, [stale]), stale, fallback=None)

    assert "stale data: RNO>LAX delta (2020-01-01)" in note_of(alert)


def test_fresh_data_adds_no_stale_note(conn):
    fresh = date.today().isoformat() + "T00:00:00Z"

    _, _, alert = alert_for(conn, [hub_option(updated=fresh)])

    assert "stale data" not in note_of(alert)


# --- freshness: alerts never fire on stale data ---------------------------------------------

def days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat() + "T00:00:00Z"


def test_an_option_resting_on_stale_data_is_never_alerted_on(conn):
    trip = add_trip(conn)

    assert decide_alert(conn, analysis_for(trip, [good_option(updated=days_ago(8))])) is None


def test_aging_data_is_still_alerted_on_but_flagged(conn):
    trip = add_trip(conn)

    alert = decide_alert(conn, analysis_for(trip, [good_option(updated=days_ago(6))]))

    assert alert is not None and f"Data as of {days_ago(6)[:10]}." in alert.message


def test_the_alert_goes_to_the_best_option_that_is_not_stale_even_if_a_stale_one_ranks_first(conn):
    trip = add_trip(conn)
    stale_first = good_option(updated=days_ago(20))
    current_second = good_option(flights=("UA9", "AV641", "AV654", "DL9"))

    alert = decide_alert(conn, analysis_for(trip, [stale_first, current_second]))

    assert alert is not None and option_key(alert.option) == option_key(current_second)


def test_the_fallback_is_also_current(conn):
    trip = add_trip(conn)
    best = good_option()
    stale_next = good_option(out_miles=15_000, flights=("UA7", "AV641", "AV654", "DL9"), updated=days_ago(30))

    alert = decide_alert(conn, analysis_for(trip, [best, stale_next]))

    assert "no other affordable option right now" in alert.message


def test_usable_for_alert_keeps_current_and_aging_and_drops_stale(conn):
    trip = add_trip(conn)
    options = [good_option(updated=days_ago(1)), good_option(updated=days_ago(7), flights=("A", "B", "C", "D")),
               good_option(updated=days_ago(8), flights=("E", "F", "G", "H"))]

    kept = usable_for_alert(analysis_for(trip, options), date.today())

    assert kept == options[:2]


# --- buy-miles sale alerts -------------------------------------------------------------------------------------------

from flight_optimizer.alerts import (  # noqa: E402
    build_promo_alert, decide_promo_alert, qualifies, qualifying_plan, record_promo_sent,
)
from flight_optimizer.promos import Promo, PromoTier  # noqa: E402

SHORT = {"delta": 14_049, "american": 26_115, "united": 3_765, "capital_one": 115_519}


def gap_option(out_miles=16_700, *, updated=RECENT, back_source="american", back_miles=10_000):
    """Delta short by out_miles - 14,049."""
    return option(
        [leg("RNO", "LAX", "delta", out_miles, JAN9, updated=updated), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, updated=updated)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, updated=updated), leg("LAX", "RNO", back_source, back_miles, JAN16, updated=updated)],
        SHORT,
    )


def delta_sale(*, tiers=(PromoTier(2_000, None, 50),), percent=50, ends=None, url="https://delta.test/sale", program="delta"):
    return Promo(key=url, program=program, kind="bonus", headline_percent=percent, tiers=tuple(tiers),
                 ends=ends or (date.today() + timedelta(days=10)), title="Delta sale", url=url, source="test",
                 last_seen=date.today())


def promo_alert_for(conn, options, promos, **kwargs):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options)
    return trip, analysis, decide_promo_alert(conn, analysis, promos, **kwargs)


def test_a_known_tier_sale_under_the_threshold_makes_an_alert(conn):
    _, _, alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})

    assert alert is not None
    assert alert.title == "Costa Rica (business): delta sale closes a gap"
    assert alert.plan.total_cost == 75.25 and alert.plan.retail_cost == 150.50


def test_the_alert_message_states_the_deal_the_total_and_what_retail_would_cost(conn):
    _, _, alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})

    lines = alert.message.split("\n")
    assert lines[0] == "RNO > SJO  Jan 09 - Jan 16  (business)"
    assert "A delta sale makes a 76,700-mile option affordable." in lines[1]
    assert any("buy 2,000 delta miles for about $75.25" in l and "50% bonus sale" in l for l in lines)
    assert "Total: 76,700 miles + $75.25 (retail would be $150.50)." in alert.message
    assert "Verify the flights before buying." in alert.message
    assert lines[-1] == "Details: python -m flight_optimizer promos"


def test_the_alert_opens_the_sale_page_and_never_includes_balances(conn):
    _, _, alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale(url="https://delta.test/sale")})

    assert alert.click == "https://delta.test/sale" and alert.priority == 3 and alert.tags == ("moneybag",)
    for secret in ("115,519", "26,115", "14,049", "balance"):
        assert secret not in alert.message.lower()
    assert len(alert.message) < 700


def test_a_headline_only_sale_is_never_alerted_on(conn):
    _, _, alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale(tiers=())})

    assert alert is None


def test_a_sale_above_the_price_threshold_is_not_worth_an_alert(conn):
    weak = delta_sale(tiers=(PromoTier(2_000, None, 10),), percent=10)   # ~3.4 cents against a 2.63 threshold

    assert promo_alert_for(conn, [gap_option()], {"delta": weak})[2] is None


def test_no_alert_without_a_sale(conn):
    assert promo_alert_for(conn, [gap_option()], {})[2] is None


def test_a_sale_for_a_program_that_is_not_short_does_nothing(conn):
    assert promo_alert_for(conn, [gap_option()], {"jetblue": delta_sale(program="jetblue")})[2] is None


def test_an_expired_sale_does_not_alert(conn):
    over = delta_sale(ends=date.today() - timedelta(days=1))

    assert promo_alert_for(conn, [gap_option()], {"delta": over})[2] is None


def test_an_option_resting_on_stale_data_is_never_worth_a_sale_alert(conn):
    stale = gap_option(updated="2020-01-01T00:00:00Z")

    assert promo_alert_for(conn, [stale], {"delta": delta_sale()})[2] is None


def test_an_already_affordable_option_needs_no_sale(conn):
    assert promo_alert_for(conn, [gap_option(10_000)], {"delta": delta_sale()})[2] is None


def test_every_purchase_in_the_plan_must_be_a_good_sale(conn):
    two_short = gap_option(back_source="united", back_miles=10_000)   # delta AND united short

    # only Delta has a sale: the United part would be bought at retail
    assert promo_alert_for(conn, [two_short], {"delta": delta_sale()})[2] is None


def test_qualifies_needs_a_sale_known_tiers_and_a_priced_program(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [gap_option()])
    from datetime import date as _date
    good = qualifying_plan(analysis, {"delta": delta_sale()}, _date.today())

    assert good is not None and all(qualifies(p) for p in good.purchases)
    retail_only = qualifying_plan(analysis, {}, _date.today())
    assert retail_only is None


def test_the_same_sale_and_option_is_not_alerted_twice(conn):
    trip, analysis, first = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})
    record_promo_sent(conn, first)

    assert decide_promo_alert(conn, analysis, {"delta": delta_sale()}) is None


def test_a_cheaper_deal_on_the_same_option_alerts_again_inside_the_window(conn):
    trip, analysis, first = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})
    # Pretend the earlier alert was for the same option and sale at a higher price.
    db.record_alert(conn, trip_id=trip["id"], alert_type="promo", dedupe_key=first.key, miles=76_700, cash_price=999.0)

    again = decide_promo_alert(conn, analysis, {"delta": delta_sale()})

    assert again is not None and again.plan.total_cost == 75.25


def test_a_dearer_or_equal_deal_is_not_repeated_inside_the_window(conn):
    trip, analysis, first = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})
    db.record_alert(conn, trip_id=trip["id"], alert_type="promo", dedupe_key=first.key, miles=76_700, cash_price=50.0)

    assert decide_promo_alert(conn, analysis, {"delta": delta_sale()}) is None


def test_the_same_alert_returns_after_the_window(conn):
    trip, analysis, first = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})
    record_promo_sent(conn, first)
    later = datetime.now(timezone.utc) + timedelta(days=RE_ALERT_DAYS + 1)
    refreshed = analysis_for(trip, [gap_option(updated=later.date().isoformat() + "T00:00:00Z")])
    sale = delta_sale(ends=later.date() + timedelta(days=5))

    assert decide_promo_alert(conn, refreshed, {"delta": sale}, now=later) is not None


def test_recording_a_promo_alert_stores_its_type_cost_and_miles(conn):
    trip, _, alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})

    record_promo_sent(conn, alert)

    row = db.last_alert(conn, trip["id"], alert.key)
    assert row["alert_type"] == "promo" and row["cash_price"] == 75.25 and row["miles"] == 76_700


def test_a_promo_alert_does_not_suppress_an_award_alert_for_the_same_trip(conn):
    trip, analysis, promo_alert = promo_alert_for(conn, [gap_option()], {"delta": delta_sale()})
    record_promo_sent(conn, promo_alert)
    affordable = analysis_for(trip, [good_option()])

    assert decide_alert(conn, affordable) is not None


# --- a cabin is only worth the miles if they beat paying cash -----------------------------------

def test_no_alert_when_the_miles_would_buy_less_than_a_cent_each_of_cash_fare(conn):
    cheap_cash = TripBaseline(cabin_fares=(fare(300), fare(300)), economy_fares=(fare(300), fare(300)))

    _, analysis, alert = alert_for(conn, [good_option()], baseline_value=cheap_cash)

    assert analysis.affordable and alert is None  # 600 of cash for 71,800 miles is 0.84 c a mile


def test_a_missing_cash_price_does_not_silence_an_award_alert(conn):
    _, _, alert = alert_for(conn, [good_option()], baseline_value=None)

    assert alert is not None


# --- better-cabin alerts, driven by cash fares ---------------------------------------------------

NOW = datetime.now(timezone.utc)  # alerts are stamped with the real clock, so 'later' is relative to it


def cabin_analyses(conn, economy=(300, 320), business=(400, 420), business_options=(), trip=None):
    trip = trip or add_trip(conn)

    def base(cabin_fares):
        return TripBaseline(cabin_fares=tuple(fare(p) for p in cabin_fares),
                            economy_fares=tuple(fare(p) for p in economy), fetched_at="2026-09-20T08:00:00Z")

    return [
        analysis_for(trip, [], baseline_value=base(economy), cabin=Cabin.ECONOMY),
        analysis_for(trip, list(business_options), baseline_value=base(business), cabin=Cabin.BUSINESS),
    ]


def test_a_business_fare_close_to_economy_makes_a_better_cabin_alert(conn):
    alert = decide_upgrade_alert(conn, cabin_analyses(conn), now=NOW)

    assert alert.title == "Costa Rica: business is only $200 more"
    lines = alert.message.split("\n")
    assert lines[0] == "RNO > SJO  Jan 09 - Jan 16"
    assert "Business by cash is $820 (out $400, back $420)" in lines[1]
    assert "1.32x" in lines[2]
    assert "Cash fares as of 2026-09-20" in alert.message


def test_a_business_fare_far_above_economy_makes_no_alert(conn):
    assert decide_upgrade_alert(conn, cabin_analyses(conn, business=(500, 500)), now=NOW) is None


def test_a_cabin_made_cheap_by_an_affordable_award_is_left_to_the_award_alert(conn):
    analyses = cabin_analyses(conn, business=(1500, 1500), business_options=[good_option()])

    assert decide_upgrade_alert(conn, analyses, now=NOW) is None  # 71,800 miles is ~$718: cheaper than its cash fare


def test_a_better_cabin_alert_is_not_repeated_within_a_week_unless_it_got_cheaper(conn):
    trip = add_trip(conn)
    first = decide_upgrade_alert(conn, cabin_analyses(conn, trip=trip), now=NOW)
    record_upgrade_sent(conn, first)

    assert decide_upgrade_alert(conn, cabin_analyses(conn, trip=trip), now=NOW + timedelta(days=2)) is None
    cheaper = cabin_analyses(conn, business=(380, 400), trip=trip)
    assert decide_upgrade_alert(conn, cheaper, now=NOW + timedelta(days=2)) is not None
    assert decide_upgrade_alert(conn, cabin_analyses(conn, trip=trip), now=NOW + timedelta(days=RE_ALERT_DAYS + 1)) is not None


# --- flexible dates ----------------------------------------------------------------------------------------------

def shifted(out_day, back_day):
    return option(
        [leg("RNO", "LAX", "delta", 10_900, out_day), leg("LAX", "SJO", "aeroplan", 25_000, out_day)],
        [leg("SJO", "LAX", "aeroplan", 25_000, back_day), leg("LAX", "RNO", "delta", 10_900, back_day)],
    )


def test_an_alert_for_shifted_dates_says_which_and_what_was_asked_for(conn):
    from datetime import date
    trip = add_trip(conn, outbound_flexible=True, return_flexible=True)
    analysis = analysis_for(trip, [shifted(date(2027, 1, 7), date(2027, 1, 14))])

    alert = decide_alert(conn, analysis)

    assert alert.message.split("\n")[0] == "RNO > SJO  Jan 07 - Jan 14 (you asked Jan 09 - Jan 16)  (business)"


def test_an_alert_on_the_asked_for_dates_does_not_mention_them_twice(conn):
    trip = add_trip(conn, outbound_flexible=True, return_flexible=True)
    analysis = analysis_for(trip, [shifted(JAN9, JAN16)])

    assert decide_alert(conn, analysis).message.split("\n")[0] == "RNO > SJO  Jan 09 - Jan 16  (business)"


def test_the_same_flights_on_different_days_are_different_alerts():
    from datetime import date
    assert option_key(shifted(date(2027, 1, 7), date(2027, 1, 14))) != option_key(shifted(JAN9, JAN16))


# --- notes about the other trips ------------------------------------------------------------------------------------

def test_notes_from_the_joint_check_are_added_to_the_alert_and_kept_on_it(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [good_option()])
    note = "Shared miles: with this booked, nothing affordable is left for Kelowna."

    alert = decide_alert(conn, analysis, notes_for=lambda option: [note])

    lines = alert.message.split("\n")
    assert note in lines and lines.index(note) < len(lines) - 1     # before the closing "Data as of" line
    assert alert.notes == (note,) and "Data as of" in lines[-1]


def test_an_alert_without_notes_reads_as_before(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [good_option()])

    plain = decide_alert(conn, analysis).message
    empty = decide_alert(conn, analysis, notes_for=lambda option: []).message

    assert plain == empty and "Shared miles" not in plain


def test_the_note_is_asked_about_the_option_being_alerted_on(conn):
    trip = add_trip(conn)
    analysis = analysis_for(trip, [good_option()])
    asked = []

    decide_alert(conn, analysis, notes_for=lambda option: asked.append(option) or [])

    assert asked == [analysis.affordable[0]]


# --- award taxes and the value floor -------------------------------------------------------------------------------

def without_taxes(opt):
    from dataclasses import replace
    from flight_optimizer.affordability import TripOption
    from flight_optimizer.itineraries import Itinerary

    return TripOption(
        itineraries=tuple(Itinerary(legs=tuple(replace(l, total_taxes=0) for l in it.legs), via=it.via) for it in opt.itineraries),
        affordability=opt.affordability,
    )


def test_taxes_can_take_an_award_below_a_cent_a_mile(conn):
    from builders import fare
    cash = TripBaseline(cabin_fares=(fare(400), fare(400)), economy_fares=(fare(400), fare(400)))    # an $800 business fare
    trip = add_trip(conn)

    free = analysis_for(trip, [without_taxes(good_option())], baseline_value=cash)       # 71,800 miles: 1.11 c each
    taxed = analysis_for(trip, [good_option()], baseline_value=cash)                      # after $305 of taxes: 0.69 c each

    assert decide_alert(conn, free) is not None
    assert decide_alert(conn, taxed) is None and taxed.affordable            # affordable, but the cash fare is the better deal


def test_the_value_line_shows_taxes_only_when_there_are_some(conn):
    trip = add_trip(conn)

    free = decide_alert(conn, analysis_for(trip, [without_taxes(good_option())]))

    assert free.message.split("\n")[1] == "71,800 miles (3.2 c/mile vs $2,267 business cash)"


def test_an_alert_leaving_the_day_before_the_asked_arrival_shows_both_days(conn):
    trip = add_trip(conn, date_anchor="arrive")  # exact dates: arrive Jan 09, return Jan 16
    early = option(
        [leg("RNO", "LAX", "delta", 10_900, date(2027, 1, 8)), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )

    alert = decide_alert(conn, analysis_for(trip, [early]))

    assert alert.message.split("\n")[0] == "RNO > SJO  Jan 09 (leave Jan 08) - Jan 16  (business)"


def test_on_a_departure_dated_trip_an_option_leaving_on_the_day_is_on_the_asked_dates(conn):
    trip = add_trip(conn)  # the default: leave Jan 09, return Jan 16
    late = option(  # leaves Jan 09, connects overnight and lands Jan 10: still the asked departure day
        [leg("RNO", "LAX", "delta", 10_900, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN10)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_900, JAN16)],
    )

    alert = decide_alert(conn, analysis_for(trip, [late]))

    assert alert.message.split("\n")[0] == "RNO > SJO  Jan 09 - Jan 16  (business)"
