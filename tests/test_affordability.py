from datetime import date, timedelta

import pytest

from flight_optimizer import db
from flight_optimizer.affordability import (
    CAPITAL_ONE_TRANSFERS,
    SOURCE_TO_PROGRAM,
    TRANSFER_TIMES,
    TripOption,
    assess,
    points_received,
    rank_trip,
    ratio_text,
    slow_transfers,
)
from flight_optimizer.itineraries import Freshness, Itinerary, TripSearchResult, leg_freshness
from flight_optimizer.seats_aero import AwardResult, Cabin

JAN9 = date(2027, 1, 9)


def leg(source, miles, origin="RNO", destination="LAX", *, stops=None, updated="2026-09-19T00:00:00Z",
        departs=None, arrives=None):
    return AwardResult(
        source=source, origin=origin, destination=destination, date=JAN9,
        cabin=Cabin.BUSINESS, miles=miles, total_taxes=0, currency="USD",
        remaining_seats=2, airlines="XX", direct=True, updated_at=updated, stops=stops,
        departs_at=departs, arrives_at=arrives,
    )


def trip(*legs, via=None):
    return Itinerary(legs=tuple(legs), via=via)


def later(itinerary, days=7):
    """The same itinerary a week on: a return that leaves after the outbound lands (the minimum stay)."""
    from dataclasses import replace
    shift = lambda moment: moment + timedelta(days=days) if moment is not None else None
    return replace(itinerary, legs=tuple(
        replace(l, date=l.date + timedelta(days=days), departs_at=shift(l.departs_at), arrives_at=shift(l.arrives_at))
        for l in itinerary.legs
    ))


def balances(**overrides):
    return {**dict.fromkeys(db.PROGRAMS, 0), **overrides}


def test_every_mapped_program_is_a_tracked_balance():
    assert set(SOURCE_TO_PROGRAM.values()) <= set(db.PROGRAMS)
    assert set(CAPITAL_ONE_TRANSFERS) <= set(db.PROGRAMS)


def test_paid_from_own_balance_needs_no_transfer():
    result = assess([trip(leg("delta", 10_900))], balances(delta=14_049))

    assert result.affordable
    assert result.transfers == {} and result.capital_one_used == 0


def test_shortfall_in_a_transfer_partner_is_topped_up_from_capital_one():
    result = assess([trip(leg("aeroplan", 25_000))], balances(capital_one=115_519))

    assert result.affordable
    assert result.transfers == {"aeroplan": 25_000}
    assert result.capital_one_used == 25_000


def test_own_balance_is_used_before_transferring():
    result = assess(
        [trip(leg("aeroplan", 25_000))], balances(aeroplan=10_000, capital_one=50_000)
    )

    assert result.transfers == {"aeroplan": 15_000}


def test_transfers_round_up_to_the_next_increment():
    result = assess([trip(leg("aeroplan", 25_001))], balances(capital_one=100_000))

    assert result.transfers == {"aeroplan": 26_000}
    assert result.capital_one_used == 26_000


def test_shortfall_in_a_non_transferable_program_is_not_affordable():
    result = assess([trip(leg("delta", 16_700))], balances(delta=14_049, capital_one=999_999))

    assert not result.affordable
    assert result.shortfalls == {"delta": 2_651}
    assert result.total_shortfall == 2_651


def test_transfers_are_capped_by_the_shared_capital_one_balance():
    result = assess(
        [trip(leg("aeroplan", 25_000)), trip(leg("lifemiles", 25_000))],
        balances(capital_one=40_000),
    )

    assert not result.affordable
    assert result.capital_one_used == 50_000
    assert result.capital_one_short == 10_000


def test_same_program_legs_across_directions_share_one_balance():
    outbound, back = trip(leg("aeroplan", 25_000)), trip(leg("aeroplan", 25_000))

    assert assess([outbound, back], balances(capital_one=50_000)).affordable
    assert not assess([outbound, back], balances(capital_one=49_000)).affordable


def test_two_legs_in_one_itinerary_are_summed_per_program():
    result = assess(
        [trip(leg("delta", 8_000), leg("delta", 7_000), via="LAX")], balances(delta=14_049)
    )

    assert result.shortfalls == {"delta": 951}


def test_unmapped_source_is_never_affordable():
    result = assess([trip(leg("mystery", 5_000))], balances(capital_one=999_999))

    assert not result.affordable
    assert result.unknown_sources == ("mystery",)
    assert result.total_shortfall == 5_000


def test_virgin_atlantic_is_reachable_through_capital_one_at_one_to_one():
    result = assess([trip(leg("virginatlantic", 21_000))], balances(capital_one=999_999))

    assert result.affordable and result.transfers == {"virginatlantic": 21_000}


def test_jetblue_takes_five_capital_one_miles_for_every_three_points():
    result = assess([trip(leg("jetblue", 21_000))], balances(capital_one=999_999))

    assert result.transfers == {"jetblue": 35_000}
    assert points_received("jetblue", 35_000) == 21_000


def test_one_more_jetblue_point_rounds_the_transfer_up_to_the_next_thousand_miles():
    result = assess([trip(leg("jetblue", 21_001))], balances(capital_one=999_999))

    assert result.transfers == {"jetblue": 36_000}
    assert points_received("jetblue", 36_000) == 21_600


def test_jetblue_is_limited_by_the_capital_one_balance_at_the_worse_ratio():
    assert assess([trip(leg("jetblue", 21_000))], balances(capital_one=35_000)).affordable
    assert not assess([trip(leg("jetblue", 21_000))], balances(capital_one=34_000)).affordable


def test_atmos_and_united_still_cannot_be_topped_up_from_capital_one():
    for source, program in (("alaska", "atmos"), ("united", "united")):
        assert program not in CAPITAL_ONE_TRANSFERS
        result = assess([trip(leg(source, 20_000))], balances(capital_one=999_999))
        assert not result.affordable and result.shortfalls == {program: 20_000}


def test_points_received_rounds_down_to_whole_points():
    assert points_received("aeroplan", 25_000) == 25_000
    assert points_received("jetblue", 1_000) == 600
    assert points_received("jetblue", 1_001) == 600


def test_ratios_are_written_the_way_capital_one_writes_them():
    assert ratio_text("qantas") == "1:1" and ratio_text("jetblue") == "5:3"


def test_every_transfer_partner_has_a_stated_transfer_time():
    assert set(CAPITAL_ONE_TRANSFERS) == set(TRANSFER_TIMES)


def test_only_non_instant_transfers_are_reported_as_slow():
    option = TripOption(
        itineraries=(trip(leg("qantas", 29_000)), trip(leg("aeroplan", 25_000)), trip(leg("jetblue", 6_000))),
        affordability=assess(
            [trip(leg("qantas", 29_000)), trip(leg("aeroplan", 25_000)), trip(leg("jetblue", 6_000))],
            balances(capital_one=999_999),
        ),
    )

    assert slow_transfers(option) == {"qantas": "up to 36 hours", "jetblue": "2 to 5 days"}


def test_a_trip_paid_from_own_balances_has_no_slow_transfers():
    it = trip(leg("delta", 5_000))

    assert slow_transfers(TripOption(itineraries=(it,), affordability=assess([it], balances(delta=9_000)))) == {}


def test_rank_puts_affordable_first_cheapest_first_then_smallest_shortfalls():
    owned = balances(delta=14_049, capital_one=100_000)
    result = TripSearchResult(
        trip_id=1,
        outbound=[
            trip(leg("delta", 16_700)),      # 2,651 short
            trip(leg("aeroplan", 30_000)),   # affordable, dearer
            trip(leg("delta", 12_000)),      # affordable, cheaper
            trip(leg("delta", 20_000)),      # 5,951 short
        ],
        return_trip=None,
    )

    ranked = rank_trip(result, owned)

    assert [o.total_miles for o in ranked] == [12_000, 30_000, 16_700, 20_000]
    assert [o.affordability.affordable for o in ranked] == [True, True, False, False]


def test_round_trip_pairs_every_outbound_with_every_return():
    result = TripSearchResult(
        trip_id=1,
        outbound=[trip(leg("delta", 10_000)), trip(leg("delta", 11_000))],
        return_trip=[later(trip(leg("aeroplan", 25_000))), later(trip(leg("aeroplan", 26_000))), later(trip(leg("aeroplan", 27_000)))],
    )

    ranked = rank_trip(result, balances(delta=20_000, capital_one=100_000))

    assert len(ranked) == 6
    assert ranked[0].total_miles == 35_000
    assert all(len(o.itineraries) == 2 for o in ranked)


def test_round_trip_is_assessed_jointly_not_per_direction():
    result = TripSearchResult(
        trip_id=1,
        outbound=[trip(leg("delta", 8_000))],
        return_trip=[later(trip(leg("delta", 8_000)))],
    )

    (option,) = rank_trip(result, balances(delta=14_049))

    assert not option.affordability.affordable  # each leg fits alone, both don't
    assert option.affordability.shortfalls == {"delta": 1_951}


def test_no_return_results_means_no_full_trip_options():
    result = TripSearchResult(trip_id=1, outbound=[trip(leg("delta", 1))], return_trip=[])

    assert rank_trip(result, balances(delta=100)) == []


def test_single_ticket_options_outrank_cheaper_self_transfers():
    result = TripSearchResult(
        trip_id=1,
        outbound=[
            trip(leg("delta", 8_000), leg("aeroplan", 10_000), via="LAX"),      # 18,000, self-transfer
            trip(leg("aeroplan", 15_000), leg("aeroplan", 15_000), via="LAX"),  # 30,000, one ticket
        ],
        return_trip=None,
    )

    ranked = rank_trip(result, balances(delta=50_000, capital_one=100_000))

    assert [o.total_miles for o in ranked] == [30_000, 18_000]
    assert [o.self_transfers for o in ranked] == [0, 1]


def test_within_the_same_self_transfer_count_cheapest_wins():
    result = TripSearchResult(
        trip_id=1,
        outbound=[trip(leg("aeroplan", 40_000)), trip(leg("aeroplan", 25_000)), trip(leg("delta", 9_000), leg("aeroplan", 1_000), via="LAX")],
        return_trip=None,
    )

    ranked = rank_trip(result, balances(delta=50_000, capital_one=100_000))

    assert [(o.self_transfers, o.total_miles) for o in ranked] == [(0, 25_000), (0, 40_000), (1, 10_000)]


def test_round_trip_counts_self_transfers_in_both_directions():
    mixed = trip(leg("delta", 5_000), leg("aeroplan", 5_000), via="LAX")
    result = TripSearchResult(trip_id=1, outbound=[mixed], return_trip=[later(mixed)])

    (option,) = rank_trip(result, balances(delta=50_000, capital_one=100_000))

    assert option.self_transfers == 2


def test_near_misses_are_still_ordered_by_shortfall_not_by_tickets():
    single_but_far = trip(leg("delta", 30_000))                        # 15,951 short
    one_ticket_close = trip(leg("delta", 10_000), leg("delta", 5_000), via="LAX")  # 951 short, same program, same day
    self_transfer_farther = trip(leg("delta", 10_000), leg("united", 5_000), via="LAX")  # united has 0, short 5,000
    result = TripSearchResult(trip_id=1, outbound=[single_but_far, self_transfer_farther, one_ticket_close], return_trip=None)

    ranked = rank_trip(result, balances(delta=14_049))

    shortfalls = [o.affordability.total_shortfall for o in ranked]
    assert shortfalls == sorted(shortfalls)


# --- extra stops and stale data in the ranking ----------------------------------------

TODAY = date(2026, 9, 20)
RICH = dict(delta=500_000, capital_one=500_000)
FRESH, OLD = "2026-09-19T00:00:00Z", "2026-08-01T00:00:00Z"


def hub(miles, *, stops=0, updated=FRESH):
    """A same-program, same-day itinerary (one ticket) with the given stops in its legs."""
    half = miles // 2
    return trip(leg("delta", half, stops=stops, updated=updated), leg("delta", miles - half, "LAX", "SJO", stops=0, updated=updated), via="LAX")


def ranked_miles(itineraries):
    result = TripSearchResult(trip_id=1, outbound=itineraries, return_trip=None)
    return [o.total_miles for o in rank_trip(result, balances(**RICH), today=TODAY)]


def test_fewer_stops_inside_the_hub_legs_beats_a_cheaper_option_with_more():
    assert ranked_miles([hub(40_000, stops=2), hub(50_000, stops=0)]) == [50_000, 40_000]


def test_stale_data_ranks_below_fresh_data_even_when_cheaper():
    assert ranked_miles([hub(40_000, updated=OLD), hub(50_000, updated=FRESH)]) == [50_000, 40_000]


def test_current_data_outranks_fewer_stops():
    assert ranked_miles([hub(40_000, stops=0, updated=OLD), hub(30_000, stops=2, updated=FRESH)]) == [30_000, 40_000]


def test_price_still_decides_between_otherwise_equal_options():
    assert ranked_miles([hub(50_000), hub(40_000)]) == [40_000, 50_000]


def test_connection_class_still_outranks_extra_stops():
    from datetime import datetime
    day = datetime(2027, 1, 9)
    comfortable_but_stops = trip(
        leg("delta", 20_000, stops=2, departs=day.replace(hour=6), arrives=day.replace(hour=10)),
        leg("delta", 20_000, "LAX", "SJO", stops=0, departs=day.replace(hour=14), arrives=day.replace(hour=20)), via="LAX")
    tight_but_nonstop = trip(
        leg("delta", 10_000, stops=0, departs=day.replace(hour=6), arrives=day.replace(hour=10)),
        leg("delta", 10_000, "LAX", "SJO", stops=0, departs=day.replace(hour=11, minute=30), arrives=day.replace(hour=17)), via="LAX")

    assert ranked_miles([tight_but_nonstop, comfortable_but_stops]) == [40_000, 20_000]


def test_self_transfers_still_outrank_stops_connections_and_price_when_data_is_equally_fresh():
    one_ticket_many_stops = hub(90_000, stops=3)
    two_tickets_clean = trip(leg("delta", 10_000, stops=0), leg("aeroplan", 10_000, "LAX", "SJO", stops=0), via="LAX")

    assert ranked_miles([two_tickets_clean, one_ticket_many_stops]) == [90_000, 20_000]


def test_option_penalties_add_up_over_both_directions():
    outbound, back = hub(40_000, stops=2, updated=OLD), hub(40_000, stops=1)
    result = TripSearchResult(trip_id=1, outbound=[outbound], return_trip=[later(back)])

    (option,) = rank_trip(result, balances(**RICH), today=TODAY)

    assert option.stop_penalty == 3 and option.stale_leg_count(TODAY) == 2


# --- data freshness: the top quality rule ------------------------------------------------

AGING = "2026-09-15T00:00:00Z"  # 5 days before TODAY


@pytest.mark.parametrize("updated, expected", [
    ("2026-09-20T05:00:00Z", Freshness.CURRENT),   # today
    ("2026-09-17T00:00:00Z", Freshness.CURRENT),   # 3 days: the edge of current
    ("2026-09-16T00:00:00Z", Freshness.AGING),     # 4 days
    ("2026-09-13T00:00:00Z", Freshness.AGING),     # 7 days: the edge of aging
    ("2026-09-12T00:00:00Z", Freshness.STALE),     # 8 days
    ("2026-08-20T00:00:00Z", Freshness.STALE),
    ("", Freshness.AGING),                         # unknown age: not confirmed current, not called stale
])
def test_leg_freshness_boundaries(updated, expected):
    assert leg_freshness(leg("delta", 1, updated=updated), TODAY) is expected


def test_tiers_are_ordered_current_then_aging_then_stale():
    assert Freshness.CURRENT < Freshness.AGING < Freshness.STALE


def test_current_beats_aging_beats_stale_when_all_else_is_equal():
    assert ranked_miles([hub(30_000, updated=OLD), hub(40_000, updated=AGING), hub(50_000, updated=FRESH)]) == [50_000, 40_000, 30_000]


def test_freshness_outranks_every_other_quality_rule():
    stale_but_ideal = hub(20_000, stops=0, updated=OLD)  # one ticket, nonstop, cheapest, but stale
    fresh_but_worse = trip(leg("delta", 30_000, stops=2), leg("aeroplan", 30_000, "LAX", "SJO", stops=1), via="LAX")

    assert ranked_miles([stale_but_ideal, fresh_but_worse]) == [60_000, 20_000]


def test_an_option_is_only_as_current_as_its_stalest_leg():
    one_old_leg = trip(leg("delta", 10_000, updated=FRESH), leg("delta", 10_000, "LAX", "SJO", updated=OLD), via="LAX")
    two_aging_legs = hub(40_000, updated=AGING)

    assert one_old_leg.freshness(TODAY) is Freshness.STALE
    assert ranked_miles([one_old_leg, two_aging_legs]) == [40_000, 20_000]


def test_option_freshness_covers_both_directions():
    result = TripSearchResult(trip_id=1, outbound=[hub(40_000, updated=FRESH)], return_trip=[later(hub(40_000, updated=OLD))])

    (option,) = rank_trip(result, balances(**RICH), today=TODAY)

    assert option.freshness(TODAY) is Freshness.STALE and option.oldest_age_days(TODAY) == 50


def test_price_and_the_other_rules_still_apply_within_a_freshness_tier():
    assert ranked_miles([hub(50_000, updated=OLD), hub(40_000, updated=OLD), hub(60_000, updated=FRESH)]) == [60_000, 40_000, 50_000]


# --- flexible dates: which outbound and return can be paired ------------------------------------------------

def on(day, *, source="delta", origin="RNO", destination="SJO", miles=10_000):
    from dataclasses import replace
    return replace(leg(source, miles, origin, destination), date=day)


def test_an_option_knows_the_days_its_first_flights_leave():
    out = trip(on(date(2027, 1, 7)))
    back = trip(on(date(2027, 1, 14), origin="SJO", destination="RNO"))
    (option,) = rank_trip(TripSearchResult(trip_id=1, outbound=[out], return_trip=[back]), balances(delta=100_000))

    assert (option.depart_date, option.return_date) == (date(2027, 1, 7), date(2027, 1, 14))


def test_a_one_way_option_has_no_return_date():
    (option,) = rank_trip(TripSearchResult(trip_id=1, outbound=[trip(on(JAN9))], return_trip=None), balances(delta=100_000))

    assert option.return_date is None


def test_a_return_that_leaves_before_the_outbound_arrives_is_not_a_trip():
    outbounds = [trip(on(date(2027, 1, 9))), trip(on(date(2027, 1, 12)))]
    returns = [trip(on(date(2027, 1, 10), origin="SJO", destination="RNO")),
               trip(on(date(2027, 1, 16), origin="SJO", destination="RNO"))]

    ranked = rank_trip(TripSearchResult(trip_id=1, outbound=outbounds, return_trip=returns), balances(delta=200_000))

    pairs = sorted((o.depart_date.day, o.return_date.day) for o in ranked)
    assert pairs == [(9, 10), (9, 16), (12, 16)]      # (12, 10) would come home before leaving


def test_a_return_on_the_day_the_outbound_lands_is_not_a_trip():
    # the owner's minimum stay is one night
    two_leg = trip(on(date(2027, 1, 9), destination="LAX"), on(date(2027, 1, 10), origin="LAX"), via="LAX")
    same_day = trip(on(date(2027, 1, 10), origin="SJO", destination="RNO"))
    next_day = trip(on(date(2027, 1, 11), origin="SJO", destination="RNO"))

    ranked = rank_trip(TripSearchResult(trip_id=1, outbound=[two_leg], return_trip=[same_day, next_day]),
                       balances(delta=200_000))

    assert [o.return_date.day for o in ranked] == [11]


def test_the_stay_is_counted_from_when_the_outbound_actually_lands():
    from dataclasses import replace
    from datetime import datetime
    # leaves Jan 9 at 22:00, lands after midnight on Jan 10: a return on Jan 10 leaves the same day it lands
    red_eye = replace(on(date(2027, 1, 9)), departs_at=datetime(2027, 1, 9, 22), arrives_at=datetime(2027, 1, 10, 6))
    same_day = trip(on(date(2027, 1, 10), origin="SJO", destination="RNO"))
    next_day = trip(on(date(2027, 1, 11), origin="SJO", destination="RNO"))

    ranked = rank_trip(TripSearchResult(trip_id=1, outbound=[trip(red_eye)], return_trip=[same_day, next_day]),
                       balances(delta=200_000))

    assert [o.return_date.day for o in ranked] == [11]
