from datetime import datetime, timedelta, timezone

import pytest

from builders import (
    JAN9, JAN10, JAN16, add_trip, analysis_for, at, baseline, good_option, leg, option,
)
from flight_optimizer import db
from flight_optimizer.alerts import (
    RE_ALERT_DAYS, assess_urgency, build_alert, decide_alert, option_key, record_sent,
)


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


def alert_for(conn, options, **kwargs):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options, **kwargs)
    return trip, analysis, decide_alert(conn, analysis)


def test_the_message_is_a_short_summary_with_urgency_and_a_pointer_to_the_plan(conn):
    trip, analysis, alert = alert_for(conn, [good_option(seats=1)])

    lines = alert.message.split("\n")
    assert lines[0] == "RNO > SJO  Jan 09 - Jan 16"
    assert lines[1] == "71,800 miles (3.2 c/mile vs $2,267 business cash)"
    assert lines[2] == "ACT TODAY: RNO>LAX delta has 1 seat left"
    assert "Full steps: python -m flight_optimizer plan 1" in lines[-1]
    assert alert.title == "Costa Rica: 71,800 miles"
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
    _, _, alert = alert_for(conn, [good_option(updated="2026-08-16T00:00:00Z")])

    assert "Data as of 2026-08-16." in alert.message


def test_without_a_cash_baseline_the_value_line_is_just_miles(conn):
    _, _, alert = alert_for(conn, [good_option()], baseline_value=None)

    assert alert.message.split("\n")[1] == "71,800 miles"


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

    assert decide_alert(conn, analysis, now=later) is not None


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
