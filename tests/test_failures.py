from datetime import datetime, timedelta, timezone

import pytest

from flight_optimizer import db
from flight_optimizer.advisor import AdvisorError
from flight_optimizer.cash_fares import SerpApiQuotaError
from flight_optimizer.failures import (
    FAILURE_REPEAT, MISSED_AFTER, Problem, build_failure_alert, decide_failure_alert, describe,
    missed_run_alert, record_attempt, record_failure_sent,
)
from flight_optimizer.seats_aero import SeatsAeroError

NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


# --- what is safe to say -------------------------------------------------------------------------------------

@pytest.mark.parametrize("error, expected", [
    (SeatsAeroError("Daily Seats.aero call quota exhausted", 429), "Daily Seats.aero call quota exhausted"),
    (SerpApiQuotaError("SerpApi search quota exhausted"), "SerpApi search quota exhausted"),
    (AdvisorError("Claude returned HTTP 401: the API key was rejected"), "Claude returned HTTP 401: the API key was rejected"),
])
def test_the_tools_own_errors_are_reported_in_their_words(error, expected):
    assert describe(error) == expected


def test_an_error_message_is_cut_short():
    assert len(describe(SeatsAeroError("x" * 1000))) == 200


@pytest.mark.parametrize("error", [ValueError("secret x-api-key: sk-123"), KeyError("password"), RuntimeError()])
def test_anything_else_is_named_by_type_only(error):
    text = describe(error)

    assert text == f"unexpected {type(error).__name__}"


# --- the alert -------------------------------------------------------------------------------------------------

def test_a_failed_trip_is_a_high_priority_alert_that_points_at_the_log():
    alert = build_failure_alert([Problem("Costa Rica", "Daily Seats.aero call quota exhausted")])

    assert alert.title == "Flight check failed" and alert.priority == 4 and "rotating_light" in alert.tags
    assert alert.message.splitlines()[0] == "- Costa Rica: Daily Seats.aero call quota exhausted"
    assert alert.message.splitlines()[-1] == "Log: logs/notify.log"


def test_several_problems_are_counted_and_listed():
    alert = build_failure_alert([Problem("Costa Rica", "a"), Problem("Kelowna", "b")])

    assert alert.title == "Flight check: 2 problems"
    assert "- Costa Rica: a" in alert.message and "- Kelowna: b" in alert.message


def test_only_degraded_extras_make_a_low_priority_alert():
    alert = build_failure_alert([Problem("Claude advice", "HTTP 429", severe=False)])

    assert alert.title == "Flight check: something is degraded" and alert.priority == 2
    assert "The check itself worked" in alert.message


def test_one_severe_problem_makes_the_whole_alert_severe():
    alert = build_failure_alert([Problem("Claude advice", "x", severe=False), Problem("Kelowna", "y")])

    assert alert.priority == 4


def test_the_key_does_not_depend_on_the_order_of_problems():
    a, b = Problem("A", "1"), Problem("B", "2")

    assert build_failure_alert([a, b]).key == build_failure_alert([b, a]).key
    assert build_failure_alert([a]).key != build_failure_alert([b]).key


# --- not repeating ----------------------------------------------------------------------------------------------

def test_no_problems_no_alert(conn):
    assert decide_failure_alert(conn, [], now=NOW) is None


def test_the_same_problem_is_quiet_for_a_day_then_reported_again(conn):
    problems = [Problem("Costa Rica", "quota")]
    first = decide_failure_alert(conn, problems, now=NOW)
    record_failure_sent(conn, first, now=NOW)

    assert decide_failure_alert(conn, problems, now=NOW + timedelta(hours=1)) is None
    assert decide_failure_alert(conn, problems, now=NOW + FAILURE_REPEAT - timedelta(minutes=1)) is None
    assert decide_failure_alert(conn, problems, now=NOW + FAILURE_REPEAT) is not None


def test_tomorrows_scheduled_run_is_not_swallowed_by_the_repeat_window(conn):
    # The job runs at 7:00 each day, a few seconds apart in the log; 20 hours leaves room either side.
    assert timedelta(hours=19) < FAILURE_REPEAT < timedelta(hours=24)


def test_a_different_problem_is_reported_at_once(conn):
    record_failure_sent(conn, decide_failure_alert(conn, [Problem("Costa Rica", "quota")], now=NOW), now=NOW)

    assert decide_failure_alert(conn, [Problem("Kelowna", "quota")], now=NOW + timedelta(minutes=5)) is not None


def test_an_alert_that_was_not_sent_is_not_remembered(conn):
    decide_failure_alert(conn, [Problem("Costa Rica", "quota")], now=NOW)   # decided, but ntfy was down, so never recorded

    assert decide_failure_alert(conn, [Problem("Costa Rica", "quota")], now=NOW) is not None


def test_a_naive_local_time_is_accepted(conn):
    problems = [Problem("Costa Rica", "quota")]
    record_failure_sent(conn, decide_failure_alert(conn, problems, now=datetime(2026, 9, 21, 7, 0)), now=datetime(2026, 9, 21, 7, 0))

    assert decide_failure_alert(conn, problems, now=datetime(2026, 9, 21, 8, 0)) is None


# --- missed runs --------------------------------------------------------------------------------------------------

def test_the_first_run_has_nothing_to_have_missed(conn):
    assert missed_run_alert(conn, now=NOW) is None


def test_a_gap_up_to_the_limit_is_not_missed_and_beyond_it_is(conn):
    record_attempt(conn, now=NOW)

    assert missed_run_alert(conn, now=NOW + timedelta(hours=25)) is None
    assert missed_run_alert(conn, now=NOW + MISSED_AFTER) is None
    assert missed_run_alert(conn, now=NOW + MISSED_AFTER + timedelta(minutes=1)) is not None


def test_the_missed_run_notice_says_how_long_and_where_to_look(conn):
    record_attempt(conn, now=NOW)

    alert = missed_run_alert(conn, now=NOW + timedelta(days=4, hours=2))

    assert alert.title == "Flight check was missed" and alert.priority == 3
    assert "about 4 days" in alert.message and "launchctl list | grep flightoptimizer" in alert.message


def test_one_missed_day_is_singular(conn):
    record_attempt(conn, now=NOW)

    assert "about 1 day " in missed_run_alert(conn, now=NOW + timedelta(hours=40)).message
