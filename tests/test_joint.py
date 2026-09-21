from datetime import date, timedelta

import pytest

from builders import JAN9, JAN16, RECENT, add_trip, analysis_for, baseline, leg, option
from flight_optimizer import db
from flight_optimizer.joint import JOINT_TOP, Candidate, build_joint, candidates, together
from flight_optimizer.report import render_joint
from flight_optimizer.seats_aero import Cabin

TODAY = date.today()
STALE = (TODAY - timedelta(days=30)).isoformat() + "T00:00:00Z"
POOL = {"capital_one": 100_000}          # only Capital One miles, to Aeroplan: every option competes for it


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def trip_a(conn):
    return add_trip(conn, name="Costa Rica")


def trip_b(conn):
    return add_trip(conn, name="Kelowna", destination="YLW")


def aeroplan(each_way, updated=RECENT, tag=""):
    """A round trip paid entirely in Aeroplan miles, `each_way` each direction."""
    return option(
        [leg("RNO", "SJO", "aeroplan", each_way, JAN9, updated=updated, flights=f"A{tag}")],
        [leg("SJO", "RNO", "aeroplan", each_way, JAN16, updated=updated, flights=f"B{tag}")],
        POOL, via=None,
    )


def analyses(trip, *, economy=(), business=(), premium=()):
    return [
        analysis_for(trip, list(economy), baseline_value=None, cabin=Cabin.ECONOMY),
        analysis_for(trip, list(premium), baseline_value=None, cabin=Cabin.PREMIUM_ECONOMY),
        analysis_for(trip, list(business), baseline_value=None, cabin=Cabin.BUSINESS),
    ]


# --- which options count ---------------------------------------------------------------------------------------

def test_only_affordable_current_options_are_candidates(conn):
    trip = trip_a(conn)
    good, too_dear, stale = aeroplan(20_000), aeroplan(70_000, tag="x"), aeroplan(10_000, updated=STALE, tag="y")

    found = candidates(analyses(trip, business=[good, too_dear, stale]), TODAY)

    assert [(c.cabin, c.option) for c in found] == [(Cabin.BUSINESS, good)]


def test_an_award_worth_less_than_the_cash_fare_is_not_a_candidate(conn):
    trip = trip_a(conn)
    cheap_cash = baseline()                     # a $2,267 business fare
    dear_award = aeroplan(45_000)               # 90,000 miles: 2.5 cents each
    silly_award = aeroplan(49_000, tag="s")     # 98,000 miles, still over a cent: kept

    found = candidates([analysis_for(trip, [dear_award, silly_award], baseline_value=cheap_cash, cabin=Cabin.BUSINESS)], TODAY)

    assert len(found) == 2

    from flight_optimizer.savings import TripBaseline
    from builders import fare
    tiny = TripBaseline(cabin_fares=(fare(300),), economy_fares=(fare(300),))
    assert candidates([analysis_for(trip, [dear_award], baseline_value=tiny, cabin=Cabin.BUSINESS)], TODAY) == []


def test_only_the_leading_options_of_each_cabin_count(conn):
    trip = trip_a(conn)
    many = [aeroplan(1_000 * (i + 1), tag=str(i)) for i in range(JOINT_TOP + 3)]

    found = candidates(analyses(trip, business=many), TODAY)

    assert len(found) == JOINT_TOP and found[0].option is many[0]


def test_candidates_come_back_economy_first(conn):
    trip = trip_a(conn)

    found = candidates(analyses(trip, business=[aeroplan(20_000)], economy=[aeroplan(5_000, tag="e")]), TODAY)

    assert [c.cabin for c in found] == [Cabin.ECONOMY, Cabin.BUSINESS]


# --- pooling two options -----------------------------------------------------------------------------------------

def test_two_options_are_assessed_on_one_set_of_balances():
    small, big = aeroplan(20_000), aeroplan(30_000, tag="b")

    assert together(small, big, POOL).affordable                       # 40,000 + 60,000 = 100,000: exactly the pool
    assert not together(big, aeroplan(30_001, tag="c"), POOL).affordable


def test_the_same_program_balance_is_shared_between_the_options():
    from flight_optimizer.affordability import assess
    delta_a = option([leg("RNO", "SJO", "delta", 15_000, JAN9)], [leg("SJO", "RNO", "delta", 15_000, JAN16)], {"delta": 30_000}, via=None)
    delta_b = option([leg("RNO", "YLW", "delta", 5_000, JAN9)], [leg("YLW", "RNO", "delta", 5_000, JAN16)], {"delta": 30_000}, via=None)

    assert delta_a.affordability.affordable and delta_b.affordability.affordable
    assert not together(delta_a, delta_b, {"delta": 30_000}).affordable   # 30,000 + 10,000 from a 30,000 balance
    assert together(delta_a, delta_b, {"delta": 40_000}).affordable


# --- the pair report -----------------------------------------------------------------------------------------------

def joint(conn, first, second, balances=POOL):
    return build_joint([first, second], balances, TODAY)


def test_trips_that_fit_together_report_the_cheapest_pair(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)], economy=[aeroplan(10_000, tag="e")])
    b = analyses(trip_b(conn), economy=[aeroplan(15_000, tag="k")], business=[aeroplan(20_000, tag="kb")])

    (pair,) = joint(conn, a, b).pairs

    assert pair.competing and not pair.conflict
    assert (pair.best.first.cabin, pair.best.second.cabin) == (Cabin.ECONOMY, Cabin.ECONOMY)
    assert pair.best.total_miles == 20_000 + 30_000


def test_trips_that_cannot_both_be_afforded_are_a_conflict(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")])
    balances = {"capital_one": 100_000}      # 60,000 each: 120,000 in all

    (pair,) = joint(conn, a, b, balances).pairs

    assert pair.competing and pair.conflict and pair.best is None


def test_a_trip_with_no_affordable_award_competes_with_nothing(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn))

    (pair,) = joint(conn, a, b).pairs

    assert not pair.competing and not pair.conflict and pair.best is None


def test_booking_one_can_leave_the_other_only_some_cabins(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])                       # 60,000
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")], economy=[aeroplan(10_000, tag="e")])  # 60,000 or 20,000
    view = joint(conn, a, b, {"capital_one": 100_000})

    (pair,) = view.pairs
    by_name = {(n.candidate.trip["name"], n.candidate.cabin): n.kept for n in pair.narrowings}

    assert by_name[("Costa Rica", Cabin.BUSINESS)] == (Cabin.ECONOMY,)          # Kelowna keeps only economy
    assert by_name[("Kelowna", Cabin.BUSINESS)] == ()                            # and Costa Rica keeps nothing
    assert ("Kelowna", Cabin.ECONOMY) not in by_name                             # 20,000 leaves Costa Rica all it had


def test_no_narrowing_is_reported_when_everything_still_fits(conn):
    a = analyses(trip_a(conn), business=[aeroplan(10_000)])
    b = analyses(trip_b(conn), business=[aeroplan(10_000, tag="k")])

    (pair,) = joint(conn, a, b).pairs

    assert pair.narrowings == () and pair.best is not None


def test_three_trips_are_compared_pair_by_pair(conn):
    trips = [analyses(trip_a(conn), business=[aeroplan(10_000)]),
             analyses(trip_b(conn), business=[aeroplan(10_000, tag="k")]),
             analyses(add_trip(conn, name="Third", destination="LHR"), business=[aeroplan(10_000, tag="t")])]

    assert len(build_joint(trips, POOL, TODAY).pairs) == 3


# --- notes for an alert or plan ----------------------------------------------------------------------------------------

def test_a_note_says_when_nothing_is_left_for_the_other_trip(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")])
    view = joint(conn, a, b, {"capital_one": 100_000})
    trip_id = a[0].trip["id"]

    notes = view.notes_for(trip_id, a[2].ranked[0])

    assert notes == ["Shared miles: with this booked, nothing affordable is left for Kelowna."]


def test_a_note_says_which_cabins_the_other_trip_keeps(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")], economy=[aeroplan(10_000, tag="e")])
    view = joint(conn, a, b, {"capital_one": 100_000})

    assert view.notes_for(a[0].trip["id"], a[2].ranked[0]) == ["Shared miles: with this booked, Kelowna keeps only economy by award."]


def test_no_note_when_the_other_trip_keeps_everything(conn):
    a = analyses(trip_a(conn), business=[aeroplan(10_000)])
    b = analyses(trip_b(conn), business=[aeroplan(10_000, tag="k")])

    assert joint(conn, a, b).notes_for(a[0].trip["id"], a[2].ranked[0]) == []


def test_a_trip_is_never_compared_with_itself_and_a_trip_without_options_adds_no_note(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn))

    assert joint(conn, a, b).notes_for(a[0].trip["id"], a[2].ranked[0]) == []


def test_notes_never_state_a_balance(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")])
    notes = joint(conn, a, b, {"capital_one": 100_000}).notes_for(a[0].trip["id"], a[2].ranked[0])

    assert not any(ch.isdigit() for ch in " ".join(notes))


# --- the report section --------------------------------------------------------------------------------------------------

def test_the_section_names_the_cheapest_pair_and_the_pool_it_uses(conn):
    a = analyses(trip_a(conn), economy=[aeroplan(10_000, tag="e")])
    b = analyses(trip_b(conn), economy=[aeroplan(15_000, tag="k")])

    text = render_joint(joint(conn, a, b))

    assert "SHARED BALANCES" in text and "Costa Rica and Kelowna" in text
    assert "Both by award: possible. Cheapest pair: Costa Rica economy 20,000 miles + Kelowna economy 30,000 miles = 50,000 miles." in text
    assert "That pair moves 50,000 Capital One miles in all." in text


def test_the_section_says_plainly_when_both_cannot_be_booked(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")])

    text = render_joint(joint(conn, a, b, {"capital_one": 100_000}))

    assert "Both by award: NOT possible" in text and "Choose which trip gets awards" in text
    assert "leaves Kelowna: nothing affordable" in text


def test_the_section_lists_what_a_booking_leaves_the_other_trip(conn):
    a = analyses(trip_a(conn), business=[aeroplan(30_000)])
    b = analyses(trip_b(conn), business=[aeroplan(30_000, tag="k")], economy=[aeroplan(10_000, tag="e")])

    text = render_joint(joint(conn, a, b, {"capital_one": 100_000}))

    assert "Costa Rica business (leading option, 60,000 miles) leaves Kelowna: only economy stay affordable." in text


def test_the_section_says_when_there_is_no_competition(conn):
    only_a = render_joint(joint(conn, analyses(trip_a(conn), business=[aeroplan(30_000)]), analyses(trip_b(conn))))
    neither = render_joint(joint(conn, analyses(add_trip(conn, name="X")), analyses(add_trip(conn, name="Y"))))

    assert "No competition: only Costa Rica has an affordable award right now." in only_a
    assert "No competition: neither has an affordable award right now." in neither
