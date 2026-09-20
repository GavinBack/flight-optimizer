from datetime import date

from flight_optimizer import db
from flight_optimizer.affordability import (
    CAPITAL_ONE_TRANSFERS,
    SOURCE_TO_PROGRAM,
    assess,
    rank_trip,
)
from flight_optimizer.itineraries import Itinerary, TripSearchResult
from flight_optimizer.seats_aero import AwardResult, Cabin

JAN9 = date(2027, 1, 9)


def leg(source, miles, origin="RNO", destination="LAX"):
    return AwardResult(
        source=source, origin=origin, destination=destination, date=JAN9,
        cabin=Cabin.BUSINESS, miles=miles, total_taxes=0, currency="USD",
        remaining_seats=2, airlines="XX", direct=True, updated_at="2026-09-19T00:00:00Z",
    )


def trip(*legs, via=None):
    return Itinerary(legs=tuple(legs), via=via)


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


def test_unverified_transfer_partners_must_be_paid_from_their_own_balance():
    assert "virginatlantic" not in CAPITAL_ONE_TRANSFERS
    assert "jetblue" not in CAPITAL_ONE_TRANSFERS

    result = assess([trip(leg("virginatlantic", 21_000))], balances(capital_one=999_999))

    assert not result.affordable and result.shortfalls == {"virginatlantic": 21_000}


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
        return_trip=[trip(leg("aeroplan", 25_000)), trip(leg("aeroplan", 26_000)), trip(leg("aeroplan", 27_000))],
    )

    ranked = rank_trip(result, balances(delta=20_000, capital_one=100_000))

    assert len(ranked) == 6
    assert ranked[0].total_miles == 35_000
    assert all(len(o.itineraries) == 2 for o in ranked)


def test_round_trip_is_assessed_jointly_not_per_direction():
    result = TripSearchResult(
        trip_id=1,
        outbound=[trip(leg("delta", 8_000))],
        return_trip=[trip(leg("delta", 8_000))],
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
    result = TripSearchResult(trip_id=1, outbound=[mixed], return_trip=[mixed])

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
