"""Per-trip goals: which cabins a trip alerts on, the most the owner would pay, and the upgrade multiple."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from builders import add_trip, analysis_for, fare, good_option
from flight_optimizer import db
from flight_optimizer.alerts import decide_alert, decide_upgrade_alert, qualifying_plan, usable_for_alert
from flight_optimizer.cabins import award_cost, compare_cabins
from flight_optimizer.goals import DEFAULT_UPGRADE_MULTIPLE, TripGoals, load_goals, save_goals, update_goals
from flight_optimizer.report import render_comparison
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import Cabin

NOW = datetime.now(timezone.utc)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def cabins(conn, goals=TripGoals(), economy=(300, 320), business=(400, 420), business_options=()):
    """Economy and business analyses of one trip, under `goals`."""
    trip = add_trip(conn)

    def base(cabin_fares):
        return TripBaseline(cabin_fares=tuple(fare(p) for p in cabin_fares),
                            economy_fares=tuple(fare(p) for p in economy), fetched_at="2026-09-20T08:00:00Z")

    return [
        replace(analysis_for(trip, [], baseline_value=base(economy), cabin=Cabin.ECONOMY), goals=goals),
        replace(analysis_for(trip, list(business_options), baseline_value=base(business), cabin=Cabin.BUSINESS),
                goals=goals),
    ]


# --- the goals themselves --------------------------------------------------------------------------------------------

def test_the_defaults_are_how_every_trip_behaved_before_goals():
    goals = TripGoals()

    assert goals.cheapest and goals.upgrade and goals.upgrade_multiple == DEFAULT_UPGRADE_MULTIPLE == 1.5
    assert all(goals.covers(c) for c in (Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS))
    assert all(goals.max_for(c) is None for c in (Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS))


def test_each_goal_covers_its_own_cabins():
    assert TripGoals(upgrade=False).covers(Cabin.ECONOMY) and not TripGoals(upgrade=False).covers(Cabin.BUSINESS)
    assert not TripGoals(cheapest=False).covers(Cabin.ECONOMY)
    assert TripGoals(cheapest=False).covers(Cabin.PREMIUM_ECONOMY) and TripGoals(cheapest=False).covers(Cabin.BUSINESS)
    assert not TripGoals().covers(Cabin.FIRST)


@pytest.mark.parametrize("fields, message", [
    (dict(cheapest=False, upgrade=False), "at least one goal"),
    (dict(cheapest_max=0), "more than \\$0"),
    (dict(business_max=-1), "more than \\$0"),
    (dict(upgrade_multiple=1.0), "more than 1"),
])
def test_impossible_goals_are_refused(fields, message):
    with pytest.raises(ValueError, match=message):
        TripGoals(**fields)


def test_goals_describe_themselves():
    goals = TripGoals(cheapest_max=600, business_max=2_000, upgrade_multiple=1.8)

    assert goals.describe() == "cheapest (max $600); upgrade: business max $2,000, flag at 1.8x economy"
    assert TripGoals(upgrade=False).describe() == "cheapest"
    assert TripGoals(cheapest=False, upgrade_multiple=None).describe() == "upgrade: no multiple flag"


def test_goals_are_saved_per_trip_and_default_when_unsaved(conn):
    one, two = add_trip(conn)["id"], add_trip(conn, name="Other", outbound_date=datetime(2027, 3, 1).date())["id"]

    save_goals(conn, one, TripGoals(cheapest=False, business_max=2_000, upgrade_multiple=None))
    update_goals(conn, one, premium_economy_max=1_200)

    assert load_goals(conn, one) == TripGoals(cheapest=False, premium_economy_max=1_200, business_max=2_000,
                                              upgrade_multiple=None)
    assert load_goals(conn, two) == TripGoals()


def test_the_database_refuses_a_trip_with_no_goal(conn):
    trip_id = add_trip(conn)["id"]

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO trip_goals (trip_id, cheapest, upgrade) VALUES (?, 0, 0)", (trip_id,))


def test_old_cash_targets_become_the_cabin_maxes(tmp_path):
    path = tmp_path / "targets.db"
    old = db.connect(path)
    db.init_schema(old)
    trip_id = add_trip(old)["id"]
    old.executescript(
        f"""
        CREATE TABLE cash_targets (trip_id INTEGER NOT NULL, cabin TEXT NOT NULL, price INTEGER NOT NULL,
                                   PRIMARY KEY (trip_id, cabin));
        INSERT INTO cash_targets VALUES ({trip_id}, 'economy', 600), ({trip_id}, 'business', 2500);
        """
    )
    old.close()

    conn = db.connect(path)
    db.init_schema(conn)
    db.init_schema(conn)  # safe to run again

    assert load_goals(conn, trip_id) == TripGoals(cheapest_max=600, business_max=2_500)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cash_targets'").fetchone() is None
    assert list(tmp_path.glob("targets-backup-*.db"))


# --- award alerts --------------------------------------------------------------------------------------------------

def test_an_award_above_the_cabins_max_is_not_alerted_on(conn):
    trip = add_trip(conn)
    option = good_option()
    cost = award_cost(option)   # 71,800 miles at 1 cent + about $305 taxes
    analysis = analysis_for(trip, [option])

    assert usable_for_alert(replace(analysis, goals=TripGoals(business_max=round(cost) + 1)), NOW.date())
    assert usable_for_alert(replace(analysis, goals=TripGoals(business_max=round(cost) - 1)), NOW.date()) == []
    assert decide_alert(conn, replace(analysis, goals=TripGoals(business_max=round(cost) - 1)), now=NOW) is None


def test_a_cabin_the_goals_leave_out_gets_no_award_or_sale_alert(conn):
    trip = add_trip(conn)
    analysis = replace(analysis_for(trip, [good_option()]), goals=TripGoals(upgrade=False))  # business left out

    assert usable_for_alert(analysis, NOW.date()) == []
    assert decide_alert(conn, analysis, now=NOW) is None
    assert qualifying_plan(analysis, {}, NOW.date()) is None


def test_an_upgrade_only_trip_sends_no_economy_award_alert(conn):
    trip = add_trip(conn)
    economy = replace(analysis_for(trip, [good_option()], cabin=Cabin.ECONOMY), goals=TripGoals(cheapest=False))

    assert decide_alert(conn, economy, now=NOW) is None


# --- better-cabin flags and alerts ---------------------------------------------------------------------------------

def test_the_trips_own_multiple_decides_what_is_an_upgrade(conn):
    # business $820 against economy $620: 1.32x
    assert compare_cabins(cabins(conn), NOW.date()).upgrades
    assert not compare_cabins(cabins(conn, TripGoals(upgrade_multiple=1.2)), NOW.date()).upgrades
    assert compare_cabins(cabins(conn, TripGoals(upgrade_multiple=1.4)), NOW.date()).upgrades


def test_no_multiple_or_no_upgrade_goal_flags_nothing(conn):
    for goals in (TripGoals(upgrade_multiple=None), TripGoals(upgrade=False)):
        analyses = cabins(conn, goals)
        assert not compare_cabins(analyses, NOW.date()).upgrades
        assert decide_upgrade_alert(conn, analyses, now=NOW) is None


def test_a_better_cabin_over_its_max_is_never_flagged(conn):
    assert not compare_cabins(cabins(conn, TripGoals(business_max=800)), NOW.date()).upgrades


def test_a_cabin_at_its_max_gets_the_cash_alert_not_also_the_multiple_alert(conn):
    analyses = cabins(conn, TripGoals(business_max=900))   # $820: within the max and within 1.5x

    assert compare_cabins(analyses, NOW.date()).upgrades          # still flagged in the report
    assert decide_upgrade_alert(conn, analyses, now=NOW) is None  # the cash alert says it instead


def test_the_better_cabin_alert_names_the_trips_multiple(conn):
    alert = decide_upgrade_alert(conn, cabins(conn, TripGoals(upgrade_multiple=1.4)), now=NOW)

    assert "your goal flags better cabins at 1.4x or less" in alert.message


# --- what the owner sees -------------------------------------------------------------------------------------------

def test_the_report_states_the_goals_and_marks_left_out_cabins_and_maxes(conn):
    text = render_comparison(compare_cabins(cabins(conn, TripGoals(cheapest=False, business_max=900)), NOW.date()))

    assert "Your goals: upgrade: business max $900, flag at 1.5x economy" in text
    economy_line = next(line for line in text.splitlines() if line.strip().startswith("economy"))
    business_line = next(line for line in text.splitlines() if line.strip().startswith("business"))
    assert "(not a goal: no alerts)" in economy_line
    assert "max $900 (within)" in business_line


def test_a_change_of_goals_asks_the_advisor_again(conn):
    from flight_optimizer.advisor import fingerprint

    analyses = cabins(conn)
    changed = [replace(a, goals=TripGoals(business_max=2_000)) for a in analyses]

    assert fingerprint([analyses], {}, None, model="m", today=NOW.date()) == fingerprint(
        [list(analyses)], {}, None, model="m", today=NOW.date())
    assert fingerprint([analyses], {}, None, model="m", today=NOW.date()) != fingerprint(
        [changed], {}, None, model="m", today=NOW.date())
