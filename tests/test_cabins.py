from datetime import date, timedelta

import pytest

from builders import RECENT, add_trip, analysis_for, fare, good_option
from flight_optimizer import db
from flight_optimizer.cabins import (
    MILE_VALUE_CENTS, UPGRADE_MAX_MULTIPLE, CabinOutlook, cabin_label, compare_cabins, outlook_for,
)
from flight_optimizer.report import render_comparison
from flight_optimizer.savings import TripBaseline
from flight_optimizer.seats_aero import Cabin

TODAY = date.today()
STALE = (TODAY - timedelta(days=30)).isoformat() + "T00:00:00Z"


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def baseline(cabin_total, economy_total=620):
    return TripBaseline(
        cabin_fares=(fare(cabin_total // 2), fare(cabin_total - cabin_total // 2)),
        economy_fares=(fare(economy_total // 2), fare(economy_total - economy_total // 2)),
    )


def analyses(conn, *, economy=620, premium=None, business=None, business_options=(), premium_options=(),
             economy_options=()):
    """One trip's analyses. A cabin whose cash price is None has no fare at all."""
    trip = add_trip(conn)
    made = [analysis_for(trip, list(economy_options), baseline_value=baseline(economy), cabin=Cabin.ECONOMY)]
    made.append(analysis_for(
        trip, list(premium_options), baseline_value=baseline(premium) if premium else None, cabin=Cabin.PREMIUM_ECONOMY))
    made.append(analysis_for(
        trip, list(business_options), baseline_value=baseline(business) if business else None, cabin=Cabin.BUSINESS))
    return made


def test_the_assumptions_are_stated_as_constants():
    assert MILE_VALUE_CENTS == 1.0 and UPGRADE_MAX_MULTIPLE == 1.5


def test_cabin_labels_are_plain_words():
    assert cabin_label(Cabin.PREMIUM_ECONOMY) == "premium economy"


def test_an_award_is_costed_at_the_mile_value_and_the_cheaper_way_wins(conn):
    outlook = outlook_for(analyses(conn, business=2000, business_options=[good_option()])[2], TODAY)

    assert outlook.cash == 2000
    assert outlook.award_cost == pytest.approx(718)      # 71,800 miles at 1 cent
    assert (outlook.best_cost, outlook.best_way) == (pytest.approx(718), "award")


def test_the_award_used_for_cost_is_the_one_with_the_fewest_miles(conn):
    dear, cheap = good_option(), good_option(out_miles=5_000, flights=("A", "B", "C", "D"))

    outlook = outlook_for(analyses(conn, business=2000, business_options=[dear, cheap])[2], TODAY)

    assert outlook.award is cheap and outlook.award.total_miles == 65_900


def test_cash_wins_when_it_is_cheaper_than_the_miles(conn):
    outlook = outlook_for(analyses(conn, business=500, business_options=[good_option()])[2], TODAY)

    assert (outlook.best_cost, outlook.best_way) == (500, "cash")


def test_an_award_on_stale_data_is_not_counted(conn):
    stale = good_option(updated=STALE)

    outlook = outlook_for(analyses(conn, business=2000, business_options=[stale])[2], TODAY)

    assert outlook.award is None and outlook.best_way == "cash"


def test_a_cabin_with_nothing_priced_is_not_offered(conn):
    outlook = outlook_for(analyses(conn)[1], TODAY)

    assert not outlook.offered and outlook.best_cost is None and outlook.best_way is None


def test_a_cabin_close_in_price_to_the_cheapest_is_an_upgrade(conn):
    comparison = compare_cabins(analyses(conn, premium=800, business=2000), TODAY)

    (upgrade,) = comparison.upgrades
    assert upgrade.outlook.cabin is Cabin.PREMIUM_ECONOMY and upgrade.over.cabin is Cabin.ECONOMY
    assert upgrade.extra == 180 and upgrade.multiple == pytest.approx(800 / 620)


def test_the_threshold_is_inclusive_and_dearer_cabins_are_not_upgrades(conn):
    at_limit = compare_cabins(analyses(conn, economy=600, premium=900), TODAY)
    over = compare_cabins(analyses(conn, economy=600, premium=902), TODAY)

    assert [u.outlook.cabin for u in at_limit.upgrades] == [Cabin.PREMIUM_ECONOMY]
    assert over.upgrades == ()


def test_every_better_cabin_that_qualifies_is_listed(conn):
    comparison = compare_cabins(analyses(conn, premium=700, business=900), TODAY)

    assert [u.outlook.cabin for u in comparison.upgrades] == [Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS]


def test_business_by_affordable_award_can_be_the_cheapest_way_and_then_nothing_is_an_upgrade(conn):
    comparison = compare_cabins(analyses(conn, economy=2000, business=3000, business_options=[good_option()]), TODAY)

    assert comparison.cheapest.cabin is Cabin.BUSINESS      # $718 of miles beats $2,000 of economy cash
    assert comparison.upgrades == ()


def test_with_nothing_priced_there_is_no_cheapest_and_no_upgrade(conn):
    made = analyses(conn)
    made[0] = analysis_for(made[0].trip, [], baseline_value=None, cabin=Cabin.ECONOMY)

    comparison = compare_cabins(made, TODAY)

    assert comparison.cheapest is None and comparison.upgrades == ()


def test_outlooks_come_back_economy_first_whatever_order_they_were_given(conn):
    comparison = compare_cabins(list(reversed(analyses(conn, premium=800, business=2000))), TODAY)

    assert [o.cabin for o in comparison.outlooks] == [Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS]


# --- the report section ------------------------------------------------------------------------------------

def text(conn, **kwargs):
    return render_comparison(compare_cabins(analyses(conn, **kwargs), TODAY))


def test_the_table_shows_each_cabin_its_cash_price_and_the_cheapest(conn):
    out = text(conn, premium=800, business=2000)

    lines = out.splitlines()
    assert "CABINS AT A GLANCE" in lines[1]
    economy = next(l for l in lines if l.lstrip().startswith("economy"))
    assert "$620" in economy and economy.endswith("cheapest")
    business = next(l for l in lines if l.lstrip().startswith("business"))
    assert "$2,000" in business and "UPGRADE" not in business


def test_a_worthwhile_upgrade_is_marked_with_its_extra_cost_and_multiple(conn):
    out = text(conn, premium=800, business=2000)

    premium = next(l for l in out.splitlines() if l.lstrip().startswith("premium economy"))
    assert "+$180 (1.29x): UPGRADE OPPORTUNITY" in premium


def test_an_affordable_award_is_shown_with_its_dollar_equivalent(conn):
    out = text(conn, business=2000, business_options=[good_option()])

    business = next(l for l in out.splitlines() if l.lstrip().startswith("business"))
    assert "71,800 miles (~$718)" in business and "$718 award" in business


def test_a_cabin_nobody_sells_says_none_found(conn):
    out = text(conn)

    premium = next(l for l in out.splitlines() if l.lstrip().startswith("premium economy"))
    assert "none found" in premium


def test_the_assumptions_behind_the_table_are_printed(conn):
    out = text(conn, business=2000)

    assert "1 cent per mile" in out and "1.5x" in out and "not counted" in out
