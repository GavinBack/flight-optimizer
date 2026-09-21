from datetime import date, datetime, timedelta, timezone

import pytest

from builders import JAN9, JAN16, add_trip, analysis_for, baseline, fare, good_option, leg, option
from flight_optimizer import db
from flight_optimizer.digest import (
    DIGEST_INTERVAL, Digest, alert_counts_for, balance_line, build_digest, digest_due, record_digest_sent,
    trip_section,
)
from flight_optimizer.promos import Promo
from flight_optimizer.savings import TripBaseline

TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
FRESH, AGING, STALE = "2026-09-20T00:00:00Z", "2026-09-15T00:00:00Z", "2026-08-01T00:00:00Z"
SHORT = {"delta": 14_049, "american": 26_115, "capital_one": 115_519}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def affordable(updated=FRESH, **kwargs):
    return good_option(updated=updated, **kwargs)


def gap(out_miles=16_700, *, updated=FRESH):
    """Delta short by out_miles - 14,049."""
    return option(
        [leg("RNO", "LAX", "delta", out_miles, JAN9, updated=updated), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, updated=updated)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, updated=updated), leg("LAX", "RNO", "american", 10_000, JAN16, updated=updated)],
        SHORT,
    )


def section(conn, options, *, counts=None, promos=None, **kwargs):
    trip = add_trip(conn)
    return trip_section(analysis_for(trip, options, **kwargs), counts or {}, promos or {}, TODAY)


def sale(program="delta", url="https://x.test/s", tiers=()):
    return Promo(key=url, program=program, kind="bonus", headline_percent=50, tiers=tuple(tiers), ends=TODAY + timedelta(days=5),
                 title="t", url=url, source="test", last_seen=TODAY)


# --- what the status says ----------------------------------------------------------------------------------

def test_the_header_names_the_trip_route_and_dates(conn):
    assert section(conn, [affordable()])[0] == "Costa Rica: RNO > SJO  Jan 09 - Jan 16"


def test_a_mix_of_fresh_and_stale_options_is_counted_by_tier(conn):
    lines = section(conn, [affordable(), affordable(updated=AGING, flights=("A", "B", "C", "D")), affordable(updated=STALE, flights=("E", "F", "G", "H"))])

    assert "3 affordable award options: 1 current, 1 aging, 1 stale." in lines
    assert "Best with usable data: 71,800 miles (3.2 c/mile vs $2,267 cash)." in lines


def test_a_single_option_is_not_pluralised(conn):
    assert "1 affordable award option: 1 current, 0 aging, 0 stale." in section(conn, [affordable()])


def test_when_every_option_is_stale_the_digest_says_silence_is_not_absence(conn):
    lines = section(conn, [affordable(updated=STALE), affordable(updated="2026-09-01T00:00:00Z", flights=("A", "B", "C", "D"))])

    text = " ".join(lines)
    assert "2 affordable award options: 0 current, 0 aging, 2 stale." in lines
    assert "None has data newer than 7 days (the freshest is 20 days old)" in text
    assert "no award alert can fire until Seats.aero refreshes it" in text
    assert "That is silence, not an absence of options." in text
    assert "Best with usable data" not in text


def test_with_nothing_affordable_it_names_the_closest_miss(conn):
    lines = section(conn, [gap()])

    assert "No affordable award options right now. Closest: 2,651 short in delta." in lines


def test_with_no_options_at_all_it_says_so_without_a_closest_miss(conn):
    assert section(conn, []) == ["Costa Rica: RNO > SJO  Jan 09 - Jan 16", "No affordable award options right now.",
                                 "Alerts sent in the last 7 days: none.", ]


# --- alerts sent this week ------------------------------------------------------------------------------------

@pytest.mark.parametrize("counts, expected", [
    ({}, "Alerts sent in the last 7 days: none."),
    ({"award": 2}, "Alerts sent in the last 7 days: 2 award."),
    ({"promo": 1}, "Alerts sent in the last 7 days: 1 sale."),
    ({"award": 1, "promo": 3}, "Alerts sent in the last 7 days: 1 award, 3 sale."),
])
def test_the_alert_line(conn, counts, expected):
    assert expected in section(conn, [affordable()], counts=counts)


# --- buying, cash, sales ----------------------------------------------------------------------------------------

def test_the_cheapest_current_gap_to_buy_is_named(conn):
    lines = section(conn, [gap()])

    assert "Cheapest gap to close by buying: 2,651 delta miles, about $150.50." in lines


def test_a_gap_priced_with_a_sale_of_known_tiers_says_a_sale_applies(conn):
    from flight_optimizer.promos import PromoTier
    lines = section(conn, [gap()], promos={"delta": sale(tiers=[PromoTier(2_000, None, 50)])})

    assert any(l.startswith("Cheapest gap to close by buying") and l.endswith("(a sale applies).") for l in lines)


def test_a_gap_priced_with_a_headline_only_sale_is_called_a_best_case(conn):
    lines = section(conn, [gap()], promos={"delta": sale()})

    assert any(l.endswith("(a sale applies, best case: its tiers are unknown).") for l in lines)


def test_a_gap_that_rests_on_stale_data_is_not_offered(conn):
    assert not any("Cheapest gap" in l for l in section(conn, [gap(updated=STALE)]))


def test_no_buy_line_when_nothing_can_be_bought_into(conn):
    assert not any("Cheapest gap" in l for l in section(conn, [affordable()]))


def test_the_cash_fare_date_is_shown_when_known(conn):
    fetched = TripBaseline(cabin_fares=(fare(1505), fare(762)), economy_fares=(fare(323), fare(376)), fetched_at="2026-09-20T07:00:00Z")

    assert "Cash fares as of 2026-09-20." in section(conn, [affordable()], baseline_value=fetched)
    assert not any("Cash fares" in l for l in section(conn, [affordable()]))


# --- balances -------------------------------------------------------------------------------------------------------

def test_balances_recently_updated_are_reported_as_current():
    line = balance_line({"delta": 14_049, "united": 3_765}, {"delta": "2026-09-20T10:00:00Z", "united": "2026-09-14T10:00:00Z"}, TODAY)

    assert line == "Balances: all updated within 7 days."


def test_an_old_balance_is_named_and_the_fix_is_given():
    line = balance_line({"delta": 14_049, "american": 26_115}, {"delta": "2026-09-20T10:00:00Z", "american": "2026-08-01T10:00:00Z"}, TODAY)

    assert line == "Balances: American AAdvantage was last updated 51 days ago. Update with: python -m flight_optimizer balance"


def test_zero_and_never_set_balances_are_not_counted_as_stale():
    line = balance_line({"delta": 14_049, "qantas": 0, "atmos": 0}, {"delta": "2026-09-20T10:00:00Z"}, TODAY)

    assert line == "Balances: all updated within 1 day."


def test_with_no_balances_the_digest_says_to_add_them():
    assert "none set" in balance_line({"delta": 0}, {}, TODAY)


def test_the_balance_line_never_states_an_amount():
    line = balance_line({"delta": 14_049}, {"delta": "2026-06-01T00:00:00Z"}, TODAY)

    assert "14,049" not in line and "14049" not in line


# --- the whole message -------------------------------------------------------------------------------------------------

def digest_for(conn, options, *, sales=None, counts=None):
    trip = add_trip(conn)
    analysis = analysis_for(trip, options)
    return build_digest(
        [analysis], alert_counts={trip["id"]: counts or {}}, promos=sales or {},
        balances=SHORT, balance_updates={"delta": "2026-09-20T10:00:00Z", "capital_one": "2026-09-20T10:00:00Z"}, today=TODAY,
    )


def test_a_single_trip_digest_is_titled_with_the_trip_and_is_low_priority(conn):
    digest = digest_for(conn, [affordable()])

    assert isinstance(digest, Digest)
    assert digest.title == "Weekly flight check: Costa Rica" and digest.priority == 2 and digest.tags == ("calendar",)


def test_the_message_ends_with_sales_balances_and_where_to_find_more(conn):
    lines = digest_for(conn, [affordable()], sales={"delta": sale(), "aeroplan": sale("aeroplan", "https://y.test")}).message.split("\n")

    assert lines[-3:] == ["Buy-miles sales on record: 2.", "Balances: all updated within 1 day.", "Full report: python -m flight_optimizer report"]


def test_no_sales_are_stated_plainly(conn):
    assert "No live buy-miles sales on record." in digest_for(conn, [affordable()]).message


def test_the_message_never_contains_balance_amounts_and_stays_short(conn):
    message = digest_for(conn, [gap(), affordable()]).message

    for amount in ("115,519", "26,115", "14,049", "115519"):
        assert amount not in message
    assert len(message) < 1_200


def test_several_trips_share_one_message_with_a_blank_line_between_them(conn):
    first = analysis_for(add_trip(conn), [affordable()])
    second = analysis_for(add_trip(conn, name="Lisbon", origin="RNO", destination="LIS"), [gap()])

    digest = build_digest([first, second], alert_counts={}, promos={}, balances=SHORT,
                          balance_updates={"delta": "2026-09-20T10:00:00Z"}, today=TODAY)

    assert digest.title == "Weekly flight check: 2 trips"
    assert "Costa Rica: RNO > SJO" in digest.message and "Lisbon: RNO > LIS" in digest.message
    assert "\n\nLisbon:" in digest.message


# --- when it is due ---------------------------------------------------------------------------------------------------------

def test_a_digest_is_due_when_none_has_ever_been_sent(conn):
    assert digest_due(conn, NOW)


def test_it_is_not_due_again_until_a_full_week_has_passed(conn):
    record_digest_sent(conn, NOW)

    assert not digest_due(conn, NOW + timedelta(days=6, hours=23, minutes=59))
    assert digest_due(conn, NOW + DIGEST_INTERVAL)


def test_a_naive_local_now_is_accepted(conn):
    record_digest_sent(conn, datetime(2026, 9, 21, 7, 0))

    assert not digest_due(conn, datetime(2026, 9, 21, 12, 0))


# --- counting the week's alerts ----------------------------------------------------------------------------------------------

def test_only_alerts_from_the_last_seven_days_are_counted(conn):
    trip = add_trip(conn)
    stamps = [("award", NOW - timedelta(days=1)), ("award", NOW - timedelta(days=6)), ("promo", NOW - timedelta(days=2)),
              ("award", NOW - timedelta(days=8))]
    for kind, when in stamps:
        db.record_alert(conn, trip_id=trip["id"], alert_type=kind, dedupe_key=f"{kind}{when}")
        conn.execute("UPDATE alert_history SET created_at = ? WHERE id = (SELECT MAX(id) FROM alert_history)",
                     (when.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.commit()

    counts = alert_counts_for(conn, [analysis_for(trip, [affordable()])], NOW)

    assert counts == {trip["id"]: {"award": 2, "promo": 1}}


def test_alerts_for_other_trips_are_not_mixed_in(conn):
    mine, other = add_trip(conn), add_trip(conn, name="Other", origin="RNO", destination="SFO")
    db.record_alert(conn, trip_id=other["id"], alert_type="award", dedupe_key="x")

    assert alert_counts_for(conn, [analysis_for(mine, [affordable()])], datetime.now(timezone.utc)) == {mine["id"]: {}}
