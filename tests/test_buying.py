from datetime import date

import pytest

from builders import JAN9, JAN16, RECENT, leg, option
from flight_optimizer import db
from flight_optimizer.buying import (
    ESTIMATE_NOTE, RETAIL_PRICES, plan_purchases, quote_purchase, rank_buy_plans,
)
from flight_optimizer.programs import PROGRAM_NAMES

TODAY = date.today()


# --- the price table ---------------------------------------------------------------------

def test_every_priced_program_is_a_tracked_program():
    assert set(RETAIL_PRICES) <= set(PROGRAM_NAMES)


def test_programs_that_cannot_or_should_not_be_bought_are_not_priced():
    assert "capital_one" not in RETAIL_PRICES  # you earn these, you don't buy them here
    assert "qantas" not in RETAIL_PRICES        # third-party sellers break Qantas's terms
    assert "southwest" not in RETAIL_PRICES     # Seats.aero has no Southwest availability


@pytest.mark.parametrize("program", sorted(RETAIL_PRICES))
def test_each_price_entry_is_sane(program):
    price = RETAIL_PRICES[program]

    assert price.program == program
    assert 1.0 < price.cents_per_point < 6.0            # cents, not dollars
    assert price.tax_rate in (0.0, 0.075)
    assert 0 < price.min_points <= 2_000 and price.step in (1_000, 2_000)
    assert price.annual_cap is None or price.annual_cap >= price.min_points
    assert price.confidence in ("medium", "low") and price.note


def test_us_carriers_carry_the_federal_excise_tax_and_the_others_do_not():
    for program in ("delta", "united", "american", "atmos", "jetblue"):
        assert RETAIL_PRICES[program].tax_rate == 0.075
    for program in ("aeroplan", "lifemiles", "british", "flyingblue", "virginatlantic"):
        assert RETAIL_PRICES[program].tax_rate == 0.0


# --- the cross-checks the table was built on ------------------------------------------------

def test_delta_reproduces_the_published_sale_arithmetic():
    assert quote_purchase("delta", 60_000).cost_usd == 2_257.50   # 60,000 x 3.5c x 1.075


def test_delta_minimum_purchase_reproduces_the_published_price():
    assert quote_purchase("delta", 1).cost_usd == 75.25            # 2,000 miles


def test_jetblue_thousand_points_reproduces_the_published_price():
    assert quote_purchase("jetblue", 1_000).cost_usd == 32.25


def test_aeroplan_is_priced_near_two_point_seven_us_cents():
    quote = quote_purchase("aeroplan", 10_000)

    assert quote.effective_cents_per_point == pytest.approx(2.7)


# --- quoting a gap ------------------------------------------------------------------------------

def test_the_real_delta_gap_is_a_4000_mile_purchase():
    quote = quote_purchase("delta", 2_651)

    assert quote.points_bought == 4_000 and quote.points_received == 4_000
    assert quote.cost_usd == 150.50
    assert quote.effective_cents_per_point == pytest.approx(3.7625)
    assert quote.points_needed == 2_651


@pytest.mark.parametrize("program, needed, expected_bought", [
    ("delta", 1, 2_000),        # below the minimum: pay for the minimum
    ("delta", 2_000, 2_000),    # exactly the minimum
    ("delta", 2_001, 4_000),    # one over: the next block of 2,000
    ("united", 2_300, 3_000),   # blocks of 1,000 above the 2,000 minimum
    ("united", 3_000, 3_000),
    ("jetblue", 1, 1_000),
    ("flyingblue", 2_001, 4_000),
])
def test_purchases_round_up_to_the_programs_block_size(program, needed, expected_bought):
    assert quote_purchase(program, needed).points_bought == expected_bought


def test_a_purchase_over_the_annual_limit_is_not_offered():
    assert quote_purchase("delta", 60_000) is not None
    assert quote_purchase("delta", 60_001) is None      # would round up to 62,000


def test_unpriced_programs_and_nonsense_amounts_have_no_quote():
    assert quote_purchase("qantas", 1_000) is None
    assert quote_purchase("delta", 0) is None
    assert quote_purchase("delta", -5) is None


def test_the_cost_is_rounded_to_whole_cents():
    quote = quote_purchase("united", 2_300)   # 3,000 x 3.5c x 1.075 = 112.875

    assert quote.cost_usd == round(quote.cost_usd, 2)


def test_a_quote_carries_the_prices_confidence_and_note():
    quote = quote_purchase("aeroplan", 2_000)

    assert quote.confidence == "low" and "exchange rate" in quote.note


# --- buying your way to an option ----------------------------------------------------------------

BALANCES = {"delta": 14_049, "united": 3_765, "american": 26_115, "capital_one": 115_519}


def near_miss(out_miles, *, back_source="american", back_miles=10_000, updated=RECENT, balances=BALANCES):
    return option(
        [leg("RNO", "LAX", "delta", out_miles, JAN9, updated=updated), leg("LAX", "SJO", "aeroplan", 25_000, JAN9, updated=updated)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16, updated=updated), leg("LAX", "RNO", back_source, back_miles, JAN16, updated=updated)],
        balances,
    )


def test_a_single_program_shortfall_becomes_a_purchase():
    plan = plan_purchases(near_miss(16_700))   # delta short 2,651

    assert [(p.program, p.shortfall, p.quote.points_bought) for p in plan.purchases] == [("delta", 2_651, 4_000)]
    assert plan.total_cost == 150.50


def test_two_short_programs_are_bought_separately_and_summed():
    plan = plan_purchases(near_miss(16_700, back_source="united", back_miles=10_000))   # united short 6,235

    assert sorted(p.program for p in plan.purchases) == ["delta", "united"]
    assert plan.total_cost == round(150.50 + quote_purchase("united", 6_235).cost_usd, 2)


def test_an_affordable_option_needs_no_purchase():
    assert plan_purchases(near_miss(10_000)) is None


def test_a_short_capital_one_pool_is_not_a_clean_fix():
    poor = {**BALANCES, "capital_one": 10_000}

    assert plan_purchases(near_miss(10_000, balances=poor)) is None


def test_a_priced_program_shortfall_is_buyable_but_a_short_capital_one_pool_is_not():
    no_atmos = option(
        [leg("RNO", "LAX", "alaska", 15_000, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_000, JAN16)],
        {**BALANCES, "atmos": 0},
    )

    plan = plan_purchases(no_atmos)   # atmos IS priced, so this is buyable: 15,000 short

    assert [p.program for p in plan.purchases] == ["atmos"]
    unpriced = option(
        [leg("RNO", "LAX", "qantas", 29_000, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_000, JAN16)],
        {**BALANCES, "capital_one": 20_000},
    )
    assert plan_purchases(unpriced) is None   # short in the Capital One pool, which cannot be bought


def test_a_program_we_cannot_price_at_all_blocks_the_plan():
    mystery = option(
        [leg("RNO", "LAX", "mystery", 5_000, JAN9), leg("LAX", "SJO", "aeroplan", 25_000, JAN9)],
        [leg("SJO", "LAX", "aeroplan", 25_000, JAN16), leg("LAX", "RNO", "delta", 10_000, JAN16)],
    )

    assert plan_purchases(mystery) is None


def test_plans_rank_current_data_before_cheaper_stale_data():
    cheap_but_stale = near_miss(16_700, updated="2020-01-01T00:00:00Z")   # $150.50
    dearer_but_current = near_miss(24_000)                                 # delta short ~9,951

    ranked = rank_buy_plans([cheap_but_stale, dearer_but_current], TODAY)

    assert [p.option for p in ranked] == [dearer_but_current, cheap_but_stale]


def test_among_equally_fresh_plans_the_cheapest_comes_first():
    ranked = rank_buy_plans([near_miss(24_000), near_miss(16_700)], TODAY)

    assert [p.total_cost for p in ranked] == sorted(p.total_cost for p in ranked)


def test_rank_ignores_options_that_buying_cannot_fix():
    fixable, fine = near_miss(16_700), near_miss(10_000)

    assert [p.option for p in rank_buy_plans([fine, fixable], TODAY)] == [fixable]


def test_the_estimate_caveat_is_stated():
    assert "estimates" in ESTIMATE_NOTE and "confirm" in ESTIMATE_NOTE


# --- prices under a sale -----------------------------------------------------------------------------------

from flight_optimizer.buying import (  # noqa: E402
    PROMO_ALERT_FRACTION, PROMO_THRESHOLDS_CENTS, promo_threshold_cents,
)
from flight_optimizer.promos import Promo, PromoTier  # noqa: E402


def sale(program="aeroplan", kind="bonus", headline=100, tiers=(), ends=date(2026, 12, 31), title="A sale", url="https://x.test/s"):
    return Promo(key=url, program=program, kind=kind, headline_percent=headline, tiers=tuple(tiers),
                 ends=ends, title=title, url=url, source="test", last_seen=TODAY)


AEROPLAN_SALE = sale(tiers=[PromoTier(5_000, 25_000, 50), PromoTier(30_000, 75_000, 75), PromoTier(80_000, None, 100)])


def test_a_bonus_sale_needs_a_smaller_purchase_to_land_the_same_points():
    quote = quote_purchase("aeroplan", 6_000, AEROPLAN_SALE, TODAY)

    assert quote.kind == "bonus" and quote.percent == 50
    assert quote.points_bought == 5_000 and quote.points_received == 7_500   # 5,000 + 50% bonus
    assert quote.cost_usd == 135.00                                           # 5,000 x 2.7 cents
    assert quote.effective_cents_per_point == pytest.approx(1.8)
    assert quote.cost_usd < quote_purchase("aeroplan", 6_000).cost_usd        # retail is $162


def test_the_tier_is_chosen_by_the_size_of_the_purchase_not_the_headline():
    small = quote_purchase("aeroplan", 3_000, AEROPLAN_SALE, TODAY)   # buys 3,000: below the lowest tier
    retail = quote_purchase("aeroplan", 3_000)

    assert small.kind == "retail" and small.cost_usd == retail.cost_usd   # the "up to 100%" does not apply


def test_a_bigger_need_jumps_past_the_gap_between_tiers_to_the_next_one():
    quote = quote_purchase("aeroplan", 40_000, AEROPLAN_SALE, TODAY)

    # 25,000 earns 50% (37,500: not enough) and 26,000-29,000 earn nothing, so the smallest
    # purchase that lands 40,000 is the 30,000 tier.
    assert quote.percent == 75 and quote.points_bought == 30_000 and quote.points_received == 52_500
    assert quote.cost_usd == 810.00 and quote.cost_usd < quote_purchase("aeroplan", 40_000).cost_usd


def test_the_smallest_sufficient_purchase_is_chosen():
    quote = quote_purchase("aeroplan", 20_000, AEROPLAN_SALE, TODAY)

    assert quote.points_bought == 14_000 and quote.points_received == 21_000   # 14,000 x 1.5
    assert quote_purchase("aeroplan", 20_000, AEROPLAN_SALE, TODAY).cost_usd == round(14_000 * 2.7 / 100, 2)


def test_a_discount_sale_lowers_the_price_and_adds_no_points():
    american = sale("american", "discount", 40, tiers=[])

    quote = quote_purchase("american", 3_000, american, TODAY)

    assert quote.kind == "discount" and quote.points_bought == quote.points_received == 3_000
    assert quote.cost_usd == round(3_000 * 3.5 / 100 * 0.6 * 1.075, 2)
    assert quote.cost_usd < quote_purchase("american", 3_000).cost_usd


def test_a_headline_only_sale_is_a_best_case_and_says_so():
    delta = sale("delta", "bonus", 50, tiers=[])

    quote = quote_purchase("delta", 2_651, delta, TODAY)

    assert not quote.tiers_known and quote.percent == 50
    assert quote.points_bought == 2_000 and quote.points_received == 3_000    # 2,000 + 50% covers 2,651
    assert quote.cost_usd == 75.25


def test_a_sale_for_another_program_is_ignored():
    assert quote_purchase("delta", 2_651, AEROPLAN_SALE, TODAY).kind == "retail"


def test_an_expired_sale_is_ignored():
    over = sale(tiers=AEROPLAN_SALE.tiers, ends=date(2026, 9, 1))

    assert quote_purchase("aeroplan", 6_000, over, TODAY).kind == "retail"


def test_a_sale_that_is_not_cheaper_falls_back_to_retail():
    pointless = sale(tiers=[PromoTier(5_000, 25_000, 0)])

    assert quote_purchase("aeroplan", 6_000, pointless, TODAY).kind == "retail"


def test_a_sale_purchase_over_the_annual_limit_is_not_offered():
    tiny_cap_sale = sale("delta", "bonus", 50, tiers=[])

    assert quote_purchase("delta", 200_000, tiny_cap_sale, TODAY) is None


def test_the_alert_threshold_is_seventy_percent_of_retail_unless_overridden():
    assert PROMO_ALERT_FRACTION == 0.70 and PROMO_THRESHOLDS_CENTS == {"lifemiles": 1.5}
    assert promo_threshold_cents("delta") == pytest.approx(3.5 * 1.075 * 0.7)
    assert promo_threshold_cents("aeroplan") == pytest.approx(2.7 * 0.7)
    assert promo_threshold_cents("lifemiles") == 1.5          # the README's own example
    assert promo_threshold_cents("qantas") is None


def test_a_buy_plan_compares_the_sale_with_retail():
    plan = plan_purchases(near_miss(16_700), {"delta": sale("delta", "bonus", 50, tiers=[])}, TODAY)

    assert plan.uses_promo and plan.total_cost == 75.25 and plan.retail_cost == 150.50
    assert plan.purchases[0].retail_quote.kind == "retail"


def test_a_plan_without_a_sale_is_all_retail():
    plan = plan_purchases(near_miss(16_700), {}, TODAY)

    assert not plan.uses_promo and plan.total_cost == plan.retail_cost == 150.50


def test_plans_rank_by_their_sale_prices():
    delta_sale = {"delta": sale("delta", "bonus", 50, tiers=[])}
    ranked = rank_buy_plans([near_miss(16_700), near_miss(24_000)], TODAY, delta_sale)

    assert ranked[0].total_cost <= ranked[1].total_cost
