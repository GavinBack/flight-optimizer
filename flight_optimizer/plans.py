"""The full booking plan for an option: the steps to read at a laptop after an alert.

Plans are saved under `plans/` (git-ignored: they contain balances and trip details).
"""

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flight_optimizer.affordability import (
    SOURCE_TO_PROGRAM, TRANSFER_NOTES, TRANSFER_TIMES, TripOption, points_received, ratio_text, slow_transfers,
)
from flight_optimizer.alerts import Urgency, assess_urgency, heads_up, oldest_data, option_key, option_legs
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.cabins import cabin_label
from flight_optimizer.programs import PROGRAM_NAMES
from flight_optimizer.report import freshness_label
from flight_optimizer.savings import estimate_value
from flight_optimizer.taxes import option_taxes, rates_note
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


def render_plan(
    analysis: TripAnalysis,
    option: TripOption,
    *,
    balances: dict[str, int],
    generated_at: datetime,
    urgency: Urgency | None = None,
    notes: tuple[str, ...] = (),
) -> str:
    trip = analysis.trip
    urgency = urgency or assess_urgency(option)
    cabin = cabin_label(analysis.cabin)
    dates = f"{option.depart_date:%b %d %Y}"
    if option.return_date is not None:
        dates += f" - {option.return_date:%b %d %Y}"
    asked = f"{date.fromisoformat(trip['depart_date']):%b %d %Y}"
    if trip["return_date"]:
        asked += f" - {date.fromisoformat(trip['return_date']):%b %d %Y}"
    if asked != dates:  # a flexible trip's option on other days
        dates += f" (you asked {asked}, flexible)"

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
            out.append(f"  {other.total_miles:,} miles ({other.total_miles - option.total_miles:+,})")
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
