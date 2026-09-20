from datetime import date

import pytest

from flight_optimizer import db
from flight_optimizer.programs import (
    ALIASES, PROGRAM_NAMES, STALE_AFTER_DAYS, format_balances, parse_miles, resolve_program,
)


def test_every_tracked_program_has_a_display_name_and_nothing_extra():
    assert tuple(PROGRAM_NAMES) == db.PROGRAMS


def test_every_alias_points_at_a_real_program():
    assert set(ALIASES.values()) <= set(PROGRAM_NAMES)


@pytest.mark.parametrize("typed, expected", [
    ("delta", "delta"), ("DELTA", "delta"), ("capital_one", "capital_one"),
    ("Capital One", "capital_one"), ("capital-one", "capital_one"), ("Capital One Miles", "capital_one"),
    ("American AAdvantage", "american"), ("aa", "american"), ("AA", "american"),
    ("skymiles", "delta"), ("MileagePlus", "united"), ("Alaska", "atmos"), ("avios", "british"),
    ("Air Canada Aeroplan", "aeroplan"), ("avianca", "lifemiles"), ("klm", "flyingblue"),
    ("c1", "capital_one"), ("Qantas Frequent Flyer", "qantas"),
])
def test_resolves_keys_names_and_aliases(typed, expected):
    assert resolve_program(typed) == expected


@pytest.mark.parametrize("typed, expected", [("cap", "capital_one"), ("virgin", "virginatlantic"), ("jet", "jetblue"), ("qan", "qantas")])
def test_a_unique_prefix_is_enough(typed, expected):
    assert resolve_program(typed) == expected


def test_an_ambiguous_prefix_lists_the_candidates():
    with pytest.raises(ValueError) as excinfo:
        resolve_program("a")

    message = str(excinfo.value)
    assert "aeroplan" in message and "american" in message and "atmos" in message


def test_an_unknown_program_lists_the_valid_ones():
    with pytest.raises(ValueError) as excinfo:
        resolve_program("hilton")

    assert "Unknown program 'hilton'" in str(excinfo.value) and "capital_one" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   ", "!!!"])
def test_an_empty_program_name_is_rejected(bad):
    with pytest.raises(ValueError):
        resolve_program(bad)


@pytest.mark.parametrize("typed, miles", [("12000", 12_000), ("12,000", 12_000), (" 115_519 ", 115_519), ("0", 0), ("007", 7)])
def test_parse_miles_accepts_whole_numbers_with_separators(typed, miles):
    assert parse_miles(typed) == miles


@pytest.mark.parametrize("bad", ["-5", "1.5", "12k", "", "abc", "1e3", "12 000"])
def test_parse_miles_rejects_everything_else(bad):
    with pytest.raises(ValueError, match="whole number of miles"):
        parse_miles(bad)


TODAY = date(2026, 9, 20)


def table(balances, updated):
    return format_balances({**dict.fromkeys(db.PROGRAMS, 0), **balances}, updated, today=TODAY).splitlines()


def test_balances_are_listed_largest_first_with_thousands_separators():
    lines = table({"capital_one": 115_519, "american": 26_115, "delta": 14_049}, {})

    assert lines[0].startswith("Program") and "Balance" in lines[0]
    assert [line.split("  ")[0] for line in lines[1:4]] == ["Capital One Miles", "American AAdvantage", "Delta SkyMiles"]
    assert "115,519" in lines[1] and "26,115" in lines[2]


def test_every_program_appears_including_zero_balances():
    lines = table({}, {})

    assert len(lines) == 1 + len(PROGRAM_NAMES)


def test_never_set_balances_say_so_and_set_ones_show_the_date():
    lines = table({"delta": 14_049}, {"delta": "2026-09-19T10:00:00Z"})

    delta = next(l for l in lines if l.startswith("Delta"))
    qantas = next(l for l in lines if l.startswith("Qantas"))
    assert "2026-09-19" in delta and "never set" not in delta
    assert "never set" in qantas


def test_old_balances_are_flagged_as_possibly_stale():
    old = "2026-08-01T10:00:00Z"  # 50 days before TODAY
    lines = table({"delta": 14_049}, {"delta": old})

    assert "not updated in 50 days" in next(l for l in lines if l.startswith("Delta"))
    fresh = table({"delta": 14_049}, {"delta": "2026-09-01T10:00:00Z"})
    assert "not updated" not in next(l for l in fresh if l.startswith("Delta"))
    assert STALE_AFTER_DAYS == 30


def test_transfer_notes_are_shown_for_capital_one_and_its_partners():
    lines = table({}, {})

    assert "transferable pool" in next(l for l in lines if l.startswith("Capital One"))
    assert "Capital One transfer 1:1" in next(l for l in lines if l.startswith("Air Canada Aeroplan"))
    assert "transfer" not in next(l for l in lines if l.startswith("Delta"))
