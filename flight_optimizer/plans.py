"""The full booking plan for an option: the steps to read at a laptop after an alert.

Plans are saved under `plans/` (git-ignored: they contain balances and trip details).
"""

import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flight_optimizer.affordability import (
    CAPITAL_ONE_TRANSFERS, SOURCE_TO_PROGRAM, TRANSFER_NOTES, TRANSFER_TIMES, TripOption, points_received, ratio_text, slow_transfers,
)
from flight_optimizer.alerts import Urgency, assess_urgency, heads_up, oldest_data, option_key, option_legs
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.buying import ESTIMATE_NOTE, MilesPrice, buy_all_cost, price_option_miles
from flight_optimizer.cabins import MILE_VALUE_CENTS, cabin_label
from flight_optimizer.programs import PROGRAM_NAMES
from flight_optimizer.promos import Promo
from flight_optimizer.report import freshness_label
from flight_optimizer.savings import estimate_value
from flight_optimizer.taxes import option_taxes, rates_note
from flight_optimizer.windows import asked_dates, on_asked_dates, option_dates
from flight_optimizer.seats_aero import TAX_UNIT_CONFIRMED, Cabin

DEFAULT_DIRECTORY = Path("plans")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _leg_line(leg) -> str:
    times = ""
    if leg.departs_at and leg.arrives_at:
        next_day = "+1" if leg.arrives_at.date() > leg.departs_at.date() else ""
        times = f" {leg.departs_at:%H:%M}-{leg.arrives_at:%H:%M}{next_day} (local)"
    flights = f"  flights {leg.flight_numbers}" if leg.flight_numbers else ""
    return f"{leg.origin}>{leg.destination}  {leg.date:%a %b %d}{times}{flights}"


def _taxes_line(leg) -> str:
    if not leg.total_taxes:
        return "taxes/fees: none reported"
    amount = f"taxes/fees: about {leg.total_taxes / 100:,.2f} {leg.currency} "
    if leg.currency in TAX_UNIT_CONFIRMED:
        return amount + f"(Seats.aero reports {leg.total_taxes}, in hundredths)"
    return amount + f"(Seats.aero reports {leg.total_taxes}; assumed hundredths, so confirm on the booking page)"


def _price_notes(price: MilesPrice) -> list[str]:
    q = price.quote
    if q is None:
        return []
    notes = []
    if q.points_bought > price.miles:
        notes.append(f"sold in blocks: buys {q.points_bought:,}")
    if q.kind != "retail":
        until = f" until {q.promo_ends:%b %d}" if q.promo_ends else ""
        notes.append(f"{q.percent}% {q.kind} sale{until}")
        if not q.tiers_known:
            notes.append("BEST CASE: this sale's tiers are not known")
        if price.retail_quote is not None:
            notes.append(f"retail {price.retail_quote.cost_usd / price.miles * 100:.2f} c/mile")
    if q.confidence == "low":
        notes.append("low-confidence price")
    return notes


def _buy_all_text(prices: tuple[MilesPrice, ...], option: TripOption, cash: int | None) -> str:
    """Buying every mile plus the taxes, against the cash fare."""
    bought, complete = buy_all_cost(prices)
    taxes = option_taxes(option)
    total = bought + taxes.usd
    plus = "" if complete and taxes.complete else "+"
    text = f"${bought:,.0f}{plus} + {taxes.text} = ${total:,.0f}{plus}"
    if cash is None:
        return text
    if not complete:  # a floor: only a verdict that the floor already settles
        return text + (f", already ${total - cash:,.0f} MORE than cash ${cash:,}" if total > cash else f" (some miles cannot be bought, so no verdict against cash ${cash:,})")
    difference = total - cash
    return text + (f", ${difference:,.0f} MORE than cash ${cash:,}" if difference > 0 else f", ${-difference:,.0f} less than cash ${cash:,}")


def _miles_vs_cash(
    analysis: TripAnalysis, option: TripOption, prices: tuple[MilesPrice, ...]
) -> list[str]:
    """Cents per mile side by side: what the miles replace in cash, and what they cost to buy or transfer."""
    cabin = cabin_label(analysis.cabin)
    value = estimate_value(option, analysis.baseline) if analysis.baseline is not None else None
    rows: list[tuple[str, str, str, str, str]] = []  # label, miles, cents per mile, cost, notes
    if value is not None and value.cents_per_mile_vs_cabin is not None:
        rows.append((
            f"cash, {cabin}", f"{option.total_miles:,}", f"{value.cents_per_mile_vs_cabin:.2f}",
            f"${value.cabin_cash:,}", f"what each mile replaces, after {value.taxes.text}",
        ))
        if analysis.cabin is not Cabin.ECONOMY and value.cents_per_mile_vs_economy is not None:
            rows.append((
                "cash, economy", f"{option.total_miles:,}", f"{value.cents_per_mile_vs_economy:.2f}",
                f"${value.economy_cash:,}", "the same, if you would otherwise fly economy",
            ))
    for price in prices:
        if price.quote is None:
            rows.append((f"buy {price.program}", f"{price.miles:,}", "-", "-", price.reason))
        else:
            rows.append((
                f"buy {price.program}", f"{price.miles:,}", f"{price.cents_per_mile:.2f}",
                f"${price.quote.cost_usd:,.0f}", "; ".join(_price_notes(price)),
            ))
        if price.program in CAPITAL_ONE_TRANSFERS:
            rate = CAPITAL_ONE_TRANSFERS[price.program]
            capital_one = math.ceil(price.miles * rate)  # Capital One miles that land `price.miles` points
            cost = capital_one * MILE_VALUE_CENTS / 100
            rows.append((
                "  or Capital One", f"{capital_one:,}", f"{cost / price.miles * 100:.2f}", f"${cost:,.0f}",
                f"transfer {rate.numerator}:{rate.denominator}, valued at the {MILE_VALUE_CENTS:.2f} c travel-eraser rate",
            ))

    width = max(len(row[0]) for row in rows) + 2
    out = ["", f"MILES VS CASH, CENTS PER MILE  ({ESTIMATE_NOTE})"]
    out.append(f"  {'':<{width}}{'miles':>9}   {'c/mile':>7}   {'cost':>10}   notes")
    if value is None or value.cents_per_mile_vs_cabin is None:
        out.append("  (no cash fare for these dates, so there is nothing to measure the miles against)")
    for label, miles, cents, cost, notes in rows:
        out.append(f"  {label:<{width}}{miles:>9}   {cents:>7}   {cost:>10}   {notes}".rstrip())
    cash = value.cabin_cash if value is not None else None
    out.append(f"  Buying every mile: {_buy_all_text(prices, option, cash)}.")
    out.append("  Paying fewer cents per mile than the cash line beats cash for those miles. Judge the whole trip")
    out.append("  by the total: one program can beat the cash line while the trip as a whole does not.")
    return out


def render_plan(
    analysis: TripAnalysis,
    option: TripOption,
    *,
    balances: dict[str, int],
    generated_at: datetime,
    urgency: Urgency | None = None,
    notes: tuple[str, ...] = (),
    promos: dict[str, Promo] | None = None,
) -> str:
    trip = analysis.trip
    urgency = urgency or assess_urgency(option)
    cabin = cabin_label(analysis.cabin)
    dates = option_dates(option, "%b %d %Y")
    if not on_asked_dates(trip, option):  # a flexible trip's option on other days
        dates += f" (you asked {asked_dates(trip, '%b %d %Y')}, flexible)"

    out = [
        f"BOOKING PLAN   {trip['name']}   {trip['origin']} > {trip['destination']}   {dates}   ({cabin})",
        f"Generated {generated_at:%Y-%m-%d %H:%M}. "
        + (f"Award data as of {oldest_data(option)} (oldest leg). " if oldest_data(option) else "")
        + "Everything below is a snapshot: confirm each flight is still bookable.",
        f"Urgency: {urgency.label}. {urgency.reason}. (Seat counts are a guide, not a guarantee.)",
        f"Data freshness: {freshness_label(option, generated_at.date())}.",
        "",
    ]

    headline = f"THE OPTION: {option.total_miles:,} miles"
    taxes = option_taxes(option)
    if taxes.usd or not taxes.complete:
        headline += f" + {taxes.text}"
    if analysis.baseline is not None:
        value = estimate_value(option, analysis.baseline)
        if value.cents_per_mile_vs_cabin is not None:
            headline += (
                f", {value.cents_per_mile_vs_cabin:.2f} c/mile vs {cabin} cash ${value.cabin_cash:,}"
            )
        if analysis.cabin is not Cabin.ECONOMY and value.cents_per_mile_vs_economy is not None:
            headline += f" ({value.cents_per_mile_vs_economy:.2f} vs economy cash ${value.economy_cash:,})"
    out.append(headline)
    bookings = sum(len(it.legs) if it.self_transfers else 1 for it in option.itineraries)
    out.append(f"  {bookings} booking{'s' if bookings != 1 else ''}.")
    for note in heads_up(option):
        out.append(f"  - {note}")

    legs = option_legs(option)
    programs = list(dict.fromkeys(SOURCE_TO_PROGRAM.get(leg.source, leg.source) for leg in legs))
    out += ["", "BEFORE YOU START: you need a free account in each program below, to receive miles and to book"]
    for program in programs:
        name = PROGRAM_NAMES.get(program, program)
        role = " (receives a Capital One transfer)" if program in option.affordability.transfers else " (paid from your own balance)"
        extra = " plus a linked Virgin Red account, which is where the transfer goes" if program == "virginatlantic" else ""
        out.append(f"  - {name}{role}{extra}")
    out += ["", "STEP 1. VERIFY every flight on the program's own site BEFORE moving any miles"]
    labels = iter("abcdefgh")
    for direction, itinerary in zip(("OUT ", "BACK"), option.itineraries):
        for leg in itinerary.legs:
            out.append(f"  1{next(labels)}. {direction} {_leg_line(leg)}")
            out.append(
                f"      {leg.source}: {leg.miles:,} miles; {leg.remaining_seats} seat"
                f"{'s' if leg.remaining_seats != 1 else ''}; {_taxes_line(leg)}"
            )
            if leg.updated_at:
                out.append(f"      data last refreshed {leg.updated_at[:10]}")
            if leg.booking_url:
                out.append(f"      open: {leg.booking_url}")
        if itinerary.connection_minutes is not None:
            out.append(f"      connection at {itinerary.via}: {_hm(itinerary.connection_minutes)}")
    out.append("  The links pre-fill route and date, not always the cabin (some open in economy): set the cabin yourself.")
    out.append("  Check that the price and the exact flights match, and note the change/cancellation terms.")

    transfers = option.affordability.transfers
    out += ["", "STEP 2. TRANSFER from Capital One (this cannot be undone)"]
    if transfers:
        for program, miles in sorted(transfers.items()):
            out.append(
                f"  Move {miles:,} Capital One miles to {program} ({ratio_text(program)}: "
                f"you receive {points_received(program, miles):,} points). Takes: {TRANSFER_TIMES[program]}."
            )
            if program in TRANSFER_NOTES:
                out.append(f"    Note: {TRANSFER_NOTES[program]}")
        left = balances.get("capital_one", 0) - option.affordability.capital_one_used
        out.append(f"  Capital One miles left afterwards: {left:,}.")
        for program, wait in sorted(slow_transfers(option).items()):
            out.append(
                f"  WARNING: the {program} transfer takes {wait}. The seats may not last that long, "
                "so check that the award space is still there when the points arrive."
            )
        out.append("  Only do this once every flight in step 1 is confirmed. Confirm the current terms in your")
        out.append("  Capital One account before you transfer.")
    else:
        out.append("  No transfer needed: every leg is paid from the program's own balance.")

    out += ["", "STEP 3. BOOK, scarcest leg first (it is the likeliest to disappear)"]
    for number, leg in enumerate(
        sorted(legs, key=lambda leg: (leg.remaining_seats, leg.updated_at or "")), start=1
    ):
        program = SOURCE_TO_PROGRAM.get(leg.source, leg.source)
        out.append(
            f"  {number}. {leg.origin}>{leg.destination} {leg.date:%b %d} with {program}: "
            f"{leg.miles:,} miles ({leg.remaining_seats} seat{'s' if leg.remaining_seats != 1 else ''} left)"
        )

    out += [
        "",
        "STEP 4. AFTER BOOKING",
        f"  Run: python -m flight_optimizer done {trip['id']}   (marks the trip inactive so alerts stop)",
    ]

    others = []
    seen = {option_key(option)}
    for other in analysis.affordable:
        key = option_key(other)
        if key not in seen:
            seen.add(key)
            others.append(other)
        if len(others) == 2:
            break
    out += ["", "IF THIS OPTION DISAPPEARS"]
    if others:
        for other in others:
            extra = ""
            if analysis.baseline is not None:
                cents = estimate_value(other, analysis.baseline).cents_per_mile_vs_cabin
                if cents is not None:
                    extra += f", {cents:.2f} c/mile vs cash"
            other_prices = price_option_miles(other, promos, generated_at.date())
            extra += f", buy every mile {_buy_all_text(other_prices, other, None)}"
            out.append(f"  {other.total_miles:,} miles ({other.total_miles - option.total_miles:+,}){extra}")
            for label, itinerary in zip(("out ", "back"), other.itineraries):
                out.append("    " + label + "  " + "  +  ".join(_leg_line(l) + f" {l.miles:,} {l.source}" for l in itinerary.legs))
    else:
        out.append("  No other affordable option right now.")

    baseline = analysis.baseline
    if baseline is not None and baseline.cabin_total is not None:
        out += [
            "",
            "CASH ALTERNATIVE  (one booking each way: bags checked through, airline covers missed connections)",
            f"  {cabin}: ${baseline.cabin_total:,} round trip"
            + (
                f"   economy: ${baseline.economy_total:,}"
                if analysis.cabin is not Cabin.ECONOMY and baseline.economy_total is not None else ""
            ),
        ]

    out += _miles_vs_cash(analysis, option, price_option_miles(option, promos, generated_at.date()))

    if notes:
        out += ["", "OTHER TRIPS SHARE THESE MILES"] + [f"  {note}" for note in notes]

    out += [
        "",
        "NOTES",
        f"  {rates_note()}",
        "  A leg reporting no taxes counts as none, though Seats.aero cannot tell free from unknown.",
        "  Legs reporting 0 seats are excluded. A connection of",
        "  1 hour or less is never recommended; under 2.5 hours is flagged. Separate bookings mean bags are",
        "  re-checked and the airline is not responsible for a missed connection between them.",
    ]
    return "\n".join(out)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "trip"


def save_plan(
    text: str, trip: sqlite3.Row, *, generated_at: datetime, cabin: Cabin = Cabin.BUSINESS,
    directory: Path = DEFAULT_DIRECTORY,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{trip['id']}-{_slug(trip['name'])}-{cabin.name.lower()}-{generated_at:%Y%m%d-%H%M}.txt"
    path.write_text(text + "\n")
    return path


def latest_plan(trip_id: int, directory: Path = DEFAULT_DIRECTORY, cabin: Cabin | None = None) -> Path | None:
    """The most recently saved plan for a trip (in `cabin`, if given), or None."""
    if not directory.exists():
        return None
    pattern = f"{trip_id}-*-{cabin.name.lower()}-*.txt" if cabin else f"{trip_id}-*.txt"
    matches = sorted(directory.glob(pattern), key=lambda path: path.stem[-13:])  # names end in YYYYMMDD-HHMM
    return matches[-1] if matches else None
