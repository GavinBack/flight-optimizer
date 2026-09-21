from dataclasses import replace

import pytest

from builders import JAN9, good_option, leg, option
from flight_optimizer.savings import TripBaseline, estimate_value
from flight_optimizer.taxes import USD_PER_UNIT, Taxes, leg_taxes_usd, option_taxes, rates_note


def taxed(currency, amount, source="aeroplan", miles=10_000):
    return replace(leg("RNO", "SJO", source, miles, JAN9), total_taxes=amount, currency=currency)


def one_way(*legs):
    return option([*legs], [], via=None)


# --- one leg -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("currency, amount, expected", [
    ("USD", 560, 5.60), ("CAD", 10_600, 76.32), ("AUD", 1_410, 9.165), ("EUR", 10_000, 108.0), ("GBP", 10_000, 127.0),
])
def test_taxes_are_hundredths_of_the_currency_converted_at_the_fixed_rate(currency, amount, expected):
    assert leg_taxes_usd(taxed(currency, amount)) == pytest.approx(expected)


def test_a_leg_with_no_taxes_counts_as_none_whatever_its_currency():
    assert leg_taxes_usd(taxed("XYZ", 0)) == 0.0


@pytest.mark.parametrize("currency", ["", "MXN", "BRL", "JPY"])
def test_a_currency_without_a_rate_is_unknown_not_guessed(currency):
    assert leg_taxes_usd(taxed(currency, 5_000)) is None


def test_the_rates_are_the_approximate_ones_stated():
    assert USD_PER_UNIT == {"USD": 1.0, "CAD": 0.72, "AUD": 0.65, "EUR": 1.08, "GBP": 1.27}


# --- a whole option -----------------------------------------------------------------------------------------------------

def test_an_options_taxes_add_up_across_legs_and_directions():
    taxes = option_taxes(good_option())        # four legs of CAD 106.00

    assert taxes.usd == pytest.approx(4 * 106 * 0.72) and taxes.complete
    assert taxes.text == "$305 taxes"


def test_mixed_currencies_are_each_converted():
    both = option([taxed("USD", 560)], [taxed("CAD", 10_000)], via=None)

    assert option_taxes(both).usd == pytest.approx(5.60 + 72.0)


def test_an_unknown_currency_makes_the_total_a_floor_and_says_so():
    partial = option([taxed("USD", 560)], [taxed("MXN", 90_000)], via=None)

    taxes = option_taxes(partial)

    assert taxes.usd == pytest.approx(5.60) and not taxes.complete
    assert taxes.text == "$6+ taxes"


def test_currencies_whose_unit_was_not_checked_are_listed():
    mixed = option([taxed("CAD", 10_600)], [taxed("USD", 560), taxed("AUD", 1_410)], via=None)

    assert option_taxes(mixed).unchecked == ("AUD", "USD")      # CAD was confirmed against aircanada.com


def test_a_leg_with_no_taxes_does_not_make_its_currency_unchecked():
    assert option_taxes(one_way(taxed("USD", 0))).unchecked == ()


def test_the_note_states_the_rates_and_what_is_confirmed():
    note = rates_note()

    assert "CAD 0.72" in note and "AUD 0.65" in note and "only CAD is confirmed" in note


# --- in the value ---------------------------------------------------------------------------------------------------------

def test_cents_per_mile_is_after_taxes():
    from builders import fare
    baseline = TripBaseline(cabin_fares=(fare(1500),), economy_fares=(fare(300),))
    free = one_way(taxed("USD", 0, miles=50_000))
    taxed_100 = one_way(taxed("USD", 10_000, miles=50_000))          # $100 of taxes

    assert estimate_value(free, baseline).cents_per_mile_vs_cabin == pytest.approx(3.0)
    value = estimate_value(taxed_100, baseline)
    assert value.cents_per_mile_vs_cabin == pytest.approx((1500 - 100) / 50_000 * 100)
    assert value.cents_per_mile_vs_economy == pytest.approx((300 - 100) / 50_000 * 100)
    assert value.taxes.usd == pytest.approx(100.0)


def test_taxes_bigger_than_the_fare_give_a_negative_value():
    from builders import fare
    baseline = TripBaseline(cabin_fares=(fare(200),), economy_fares=(fare(200),))

    assert estimate_value(one_way(taxed("USD", 30_000, miles=10_000)), baseline).cents_per_mile_vs_cabin < 0
