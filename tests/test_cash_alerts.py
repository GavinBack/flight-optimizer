from datetime import date, datetime, timedelta, timezone

import pytest

from builders import add_trip, analysis_for, fare
from flight_optimizer import db
from flight_optimizer.cash_alerts import (
    DROP_DOLLARS, DROP_PERCENT, MIN_HISTORY, decide_cash_alert, is_drop, at_target, record_cash_alert_sent,
)
from flight_optimizer.cashtrend import CashTrend, load_trend
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import Cabin

NOW = datetime.now(timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"


def stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime(FMT)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def trend(current, previous):
    return CashTrend(current=current, previous=tuple(previous), first_seen=date(2026, 9, 10) if previous else None)


def cash_analysis(conn, total, previous=(), *, cabin=Cabin.ECONOMY, target=None, trip=None, flexible=False, fetched="2026-09-21T06:00:00Z"):
    trip = trip or add_trip(conn, flexible=flexible)
    out, back = total // 2, total - total // 2
    base = TripBaseline(cabin_fares=(fare(out), fare(back)), economy_fares=(fare(out), fare(back)),
                        fetched_at=fetched, cabin_fetched_at=fetched)
    return analysis_for(trip, [], baseline_value=base, cabin=cabin), trend(total, previous), target


def analysis(conn, total, previous=(), **kwargs):
    from dataclasses import replace
    a, t, target = cash_analysis(conn, total, previous, **kwargs)
    return replace(a, cash_trend=t, cash_target=target)


# --- the history ---------------------------------------------------------------------------------------------------

def test_a_fare_is_compared_with_the_earlier_searches_and_not_with_itself(conn):
    trip = add_trip(conn)
    for days, total in ((6, 720), (4, 700), (2, 690)):
        db.record_cash_total(conn, trip["id"], "economy", stamp(days), total)

    result = load_trend(conn, trip["id"], "economy", total=640, fetched_at=stamp(0), now=NOW)

    assert result.previous == (720, 700, 690) and result.count == 3
    assert result.low == 690 and result.typical == 700 and result.current == 640
    assert result.first_seen == (NOW - timedelta(days=6)).date()


def test_the_same_search_seen_again_is_one_row_and_is_not_history_for_itself(conn):
    trip = add_trip(conn)
    load_trend(conn, trip["id"], "economy", total=700, fetched_at=stamp(1), now=NOW)

    again = load_trend(conn, trip["id"], "economy", total=700, fetched_at=stamp(1), now=NOW)

    assert again.count == 0
    assert conn.execute("SELECT COUNT(*) FROM cash_history").fetchone()[0] == 1


def test_history_older_than_thirty_days_is_ignored(conn):
    trip = add_trip(conn)
    db.record_cash_total(conn, trip["id"], "economy", stamp(45), 500)
    db.record_cash_total(conn, trip["id"], "economy", stamp(5), 700)

    assert load_trend(conn, trip["id"], "economy", total=690, fetched_at=stamp(0), now=NOW).previous == (700,)


def test_each_cabin_has_its_own_history(conn):
    trip = add_trip(conn)
    db.record_cash_total(conn, trip["id"], "business", stamp(3), 2_200)

    assert load_trend(conn, trip["id"], "economy", total=700, fetched_at=stamp(0), now=NOW).count == 0


def test_the_first_search_has_nothing_to_compare_with(conn):
    result = load_trend(conn, add_trip(conn)["id"], "economy", total=700, fetched_at=stamp(0), now=NOW)

    assert (result.count, result.low, result.typical, result.first_seen) == (0, None, None, None)


def test_the_typical_fare_is_the_median(conn):
    assert trend(0, [600, 700, 900]).typical == 700 and trend(0, [600, 700]).typical == 650


def test_moving_a_trips_dates_clears_its_history_but_a_rename_does_not(conn):
    trip = add_trip(conn)
    db.record_cash_total(conn, trip["id"], "economy", stamp(3), 700)

    db.update_trip(conn, trip["id"], name="Renamed", flexible=True)
    assert conn.execute("SELECT COUNT(*) FROM cash_history").fetchone()[0] == 1

    db.update_trip(conn, trip["id"], depart_date=date(2027, 1, 10))
    assert conn.execute("SELECT COUNT(*) FROM cash_history").fetchone()[0] == 0


# --- the drop rule -------------------------------------------------------------------------------------------------

def test_the_rule_constants_are_the_stated_ones():
    assert (DROP_PERCENT, DROP_DOLLARS, MIN_HISTORY) == (10, 50, 3)


def test_a_real_drop_to_a_new_low_is_a_drop(conn):
    assert is_drop(analysis(conn, 620, [700, 720, 690, 710]))


@pytest.mark.parametrize("current, previous", [
    (660, [700, 720, 690, 710]),          # only 6% under typical
    (695, [700, 720, 690, 710]),          # not below the previous low of 690
    (640, [700, 720]),                    # too little history
    (30, [60, 70, 65]),                   # 50% under typical, but only $35: below the dollar floor
    (700, [700, 700, 700]),               # unchanged
])
def test_ordinary_moves_are_not_drops(conn, current, previous):
    assert not is_drop(analysis(conn, current, previous))


def test_a_slow_drift_down_is_not_a_drop(conn):
    # 780, 760, 740, 720 ... each a few percent under the last, but the median moves with it.
    assert not is_drop(analysis(conn, 700, [780, 760, 740, 720]))


def test_only_economy_drops_automatically(conn):
    assert is_drop(analysis(conn, 620, [700, 720, 690, 710], cabin=Cabin.ECONOMY))
    assert not is_drop(analysis(conn, 620, [700, 720, 690, 710], cabin=Cabin.BUSINESS))
    assert not is_drop(analysis(conn, 620, [700, 720, 690, 710], cabin=Cabin.PREMIUM_ECONOMY))


def test_the_thresholds_are_inclusive(conn):
    assert is_drop(analysis(conn, 630, [700, 700, 700]))      # exactly 10% and $70 under: new low, on the line
    assert not is_drop(analysis(conn, 631, [700, 700, 700]))


# --- the target rule ---------------------------------------------------------------------------------------------

def test_a_fare_at_or_under_the_target_qualifies_in_any_cabin(conn):
    assert at_target(analysis(conn, 700, target=700, cabin=Cabin.BUSINESS))
    assert at_target(analysis(conn, 650, target=700, cabin=Cabin.PREMIUM_ECONOMY))
    assert not at_target(analysis(conn, 701, target=700))


def test_no_target_means_no_target_alert(conn):
    assert not at_target(analysis(conn, 100, target=None))


# --- the alert -----------------------------------------------------------------------------------------------------

def test_a_drop_alert_states_the_fare_the_history_and_the_date(conn):
    alert = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710]), now=NOW)

    assert alert.kind == "drop" and alert.price == 620 and alert.priority == 3
    assert alert.title == "Costa Rica: economy cash fare dropped: $620"
    lines = alert.message.split("\n")
    assert lines[0] == "RNO > SJO  Jan 09 - Jan 16  (economy cash)"
    assert lines[1] == "$620 round trip (out $310, back $310)"
    assert "Typical lately: $705 (median of 4 searches since Sep 10); lowest before: $690." in lines
    assert "Cash fares as of 2026-09-21. Fares change fast: verify before booking." in lines


def test_a_target_alert_names_the_target(conn):
    alert = decide_cash_alert(conn, analysis(conn, 690, target=700, cabin=Cabin.BUSINESS), now=NOW)

    assert alert.kind == "target" and alert.title == "Costa Rica: business cash fare at your $700 target: $690"
    assert "At or under your $700 target." in alert.message.split("\n")
    assert "(business cash)" in alert.message.split("\n")[0]


def test_a_target_wins_when_the_fare_is_also_a_drop(conn):
    alert = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], target=650), now=NOW)

    assert alert.kind == "target" and "Typical lately" in alert.message


def test_nothing_notable_means_no_alert(conn):
    assert decide_cash_alert(conn, analysis(conn, 700, [700, 720, 690, 710]), now=NOW) is None


def test_no_cash_baseline_means_no_alert(conn):
    trip = add_trip(conn)
    assert decide_cash_alert(conn, analysis_for(trip, [], baseline_value=None, cabin=Cabin.ECONOMY), now=NOW) is None


def test_a_flexible_trip_says_the_fare_is_for_exact_dates(conn):
    alert = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], flexible=True), now=NOW)

    assert "This fare is for your exact dates; flexible dates apply to awards only." in alert.message


def test_the_alert_never_includes_balances(conn):
    alert = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], target=650), now=NOW)

    assert not any(word in (alert.title + alert.message).lower() for word in ("balance", "capital one", "miles"))


# --- not repeating -----------------------------------------------------------------------------------------------------

def test_the_same_news_is_not_repeated_within_a_week_unless_the_fare_fell_further(conn):
    trip = add_trip(conn)
    first = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip), now=NOW)
    record_cash_alert_sent(conn, first)

    assert decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip), now=NOW + timedelta(days=2)) is None
    assert decide_cash_alert(conn, analysis(conn, 640, [700, 720, 690, 710], trip=trip), now=NOW + timedelta(days=2)) is None
    assert decide_cash_alert(conn, analysis(conn, 600, [700, 720, 690, 710, 620], trip=trip), now=NOW + timedelta(days=2)) is not None
    assert decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip), now=NOW + timedelta(days=8)) is not None


def test_a_drop_and_a_target_are_separate_alerts(conn):
    trip = add_trip(conn)
    drop = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip), now=NOW)
    record_cash_alert_sent(conn, drop)

    target = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip, target=650), now=NOW + timedelta(hours=1))

    assert target is not None and target.key != drop.key


def test_cabins_do_not_silence_each_other(conn):
    trip = add_trip(conn)
    econ = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710], trip=trip, target=650), now=NOW)
    record_cash_alert_sent(conn, econ)

    business = decide_cash_alert(conn, analysis(conn, 1_400, trip=trip, target=1_500, cabin=Cabin.BUSINESS), now=NOW)

    assert business is not None and business.key != econ.key


def test_sent_alerts_are_recorded_as_cash_alerts_with_their_price(conn):
    alert = decide_cash_alert(conn, analysis(conn, 620, [700, 720, 690, 710]), now=NOW)

    record_cash_alert_sent(conn, alert)

    row = conn.execute("SELECT * FROM alert_history").fetchone()
    assert (row["alert_type"], row["cash_price"], row["dedupe_key"]) == ("cash", 620, "cash:ECONOMY:drop")


# --- targets in the database --------------------------------------------------------------------------------------------

def test_targets_are_saved_replaced_and_cleared_per_cabin(conn):
    trip = add_trip(conn)
    db.set_cash_target(conn, trip["id"], "economy", 700)
    db.set_cash_target(conn, trip["id"], "business", 1_500)
    db.set_cash_target(conn, trip["id"], "economy", 650)

    assert db.get_cash_targets(conn, trip["id"]) == {"economy": 650, "business": 1_500}
    assert db.clear_cash_target(conn, trip["id"], "economy") is True
    assert db.clear_cash_target(conn, trip["id"], "economy") is False
    assert db.get_cash_targets(conn, trip["id"]) == {"business": 1_500}


def test_bad_targets_are_refused(conn):
    trip = add_trip(conn)

    with pytest.raises(ValueError, match="more than zero"):
        db.set_cash_target(conn, trip["id"], "economy", 0)
    with pytest.raises(ValueError, match="Unknown cabin"):
        db.set_cash_target(conn, trip["id"], "steerage", 100)
    with pytest.raises(LookupError):
        db.set_cash_target(conn, 999, "economy", 100)
