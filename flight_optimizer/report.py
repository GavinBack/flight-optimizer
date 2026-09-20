"""Plain-text report: ranked award options for each active trip, valued against cash."""

import sqlite3
from datetime import date

from flight_optimizer import db
from flight_optimizer.affordability import (
    CAPITAL_ONE_TRANSFERS,
    SOURCE_TO_PROGRAM,
    TripOption,
    rank_trip,
)
from flight_optimizer.cash_fares import (
    DEFAULT_MAX_LAYOVER_MINUTES,
    DEFAULT_MAX_TOTAL_MINUTES,
    CashFare,
    SerpApiClient,
    SerpApiError,
)
from flight_optimizer.itineraries import Itinerary, search_trip
from flight_optimizer.savings import TripBaseline, estimate_value, trip_baseline
from flight_optimizer.seats_aero import AwardResult, SeatsAeroClient

STALE_AWARD_DAYS = 7
RULE = "=" * 78


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _stops(count: int) -> str:
    return "nonstop" if count == 0 else f"{count} stop{'s' if count > 1 else ''}"


def _fare_brief(fare: CashFare | None) -> str:
    if fare is None:
        return "none met the limits"
    airlines = "/".join(dict.fromkeys(fare.airlines))
    return f"${fare.price:,} ({_hm(fare.total_duration)}, {_stops(fare.stops)}, {airlines})"


def _itinerary_text(itinerary: Itinerary) -> str:
    return "  +  ".join(
        f"{leg.origin}>{leg.destination} {leg.date:%b %d} {leg.miles:>6,} "
        f"{leg.source}{'' if leg.direct else '*'}"
        for leg in itinerary.legs
    )


def _booking_text(itinerary: Itinerary) -> str:
    first = itinerary.legs[0]
    if len(itinerary.legs) == 1:
        return f"1 booking ({first.source})"
    second = itinerary.legs[1]
    if itinerary.self_transfers == 0:
        return f"1 booking ({first.source}), connecting at {itinerary.via}"
    reason = (
        f"{first.source} + {second.source}"
        if first.source != second.source
        else f"{first.source}, overnight stop"
    )
    return f"2 bookings, self-transfer at {itinerary.via} ({reason})"


def _tickets_line(option: TripOption) -> str:
    parts = "; ".join(
        f"{label} {_booking_text(it)}"
        for label, it in zip(("out", "back"), option.itineraries)
    )
    note = "  [bags re-checked, no missed-connection cover there]" if option.self_transfers else ""
    return f"tickets: {parts}{note}"


def _preference_note(affordable: list[TripOption]) -> str | None:
    """What preferring fewer self-transfers costs versus the cheapest affordable option."""
    if not affordable:
        return None
    best, cheapest = affordable[0], min(affordable, key=lambda o: o.total_miles)
    if best.self_transfers > 0:
        return "No fully single-ticket option is affordable; every option below has a self-transfer."
    if best.total_miles > cheapest.total_miles:
        extra = best.total_miles - cheapest.total_miles
        return (
            f"Ranked single-ticket first: #1 costs {extra:,} more miles than the cheapest "
            f"affordable option ({cheapest.total_miles:,} miles, "
            f"{cheapest.self_transfers} self-transfer{'s' if cheapest.self_transfers != 1 else ''})."
        )
    return None


def _shape(option: TripOption) -> tuple:
    """Everything about an option except dates, to collapse same-price date variants."""
    return tuple(
        tuple((l.origin, l.destination, l.source, l.miles, l.direct) for l in it.legs)
        for it in option.itineraries
    )


def _group_by_shape(options: list[TripOption]) -> list[tuple[TripOption, int]]:
    """Ranked options with same-shape date variants folded into (first, variant_count)."""
    groups: dict[tuple, list[TripOption]] = {}
    for option in options:
        groups.setdefault(_shape(option), []).append(option)
    return [(members[0], len(members) - 1) for members in groups.values()]


def _leg_name(leg: AwardResult) -> str:
    return f"{leg.origin}>{leg.destination} {leg.source}"


def _warnings(option: TripOption, today: date) -> list[str]:
    """One line per concern, naming the legs involved."""
    itineraries = option.itineraries
    legs = [leg for it in itineraries for leg in it.legs]
    warnings = []

    connecting = [_leg_name(leg) for leg in legs if not leg.direct]
    if connecting:
        warnings.append("connecting flights (*), check the routing: " + ", ".join(connecting))

    same_day = [
        f"{it.via or it.legs[0].destination} on {it.legs[0].date:%b %d}"
        for it in itineraries
        if len(it.legs) == 2 and it.legs[0].date == it.legs[1].date
    ]
    if same_day:
        warnings.append("same-day connection at " + ", ".join(same_day) + " (flight times not checked)")

    no_seats = [_leg_name(leg) for leg in legs if leg.remaining_seats == 0]
    if no_seats:
        warnings.append("0 seats reported: " + ", ".join(no_seats))

    stale = [
        f"{_leg_name(leg)} ({leg.updated_at[:10]})"
        for leg in legs
        if leg.updated_at and (today - date.fromisoformat(leg.updated_at[:10])).days > STALE_AWARD_DAYS
    ]
    if stale:
        warnings.append(f"data older than {STALE_AWARD_DAYS} days, last refreshed: " + ", ".join(stale))
    return warnings


def _payment_lines(option: TripOption, balances: dict[str, int]) -> list[str]:
    needs: dict[str, int] = {}
    for it in option.itineraries:
        for source, miles in it.miles_by_program.items():
            program = SOURCE_TO_PROGRAM.get(source, source)
            needs[program] = needs.get(program, 0) + miles

    transfers = option.affordability.transfers
    own = {
        program: (need if program not in transfers else balances.get(program, 0))
        for program, need in needs.items()
    }
    lines = []
    own_parts = [f"{program} {miles:,}" for program, miles in sorted(own.items()) if miles]
    if own_parts:
        lines.append("pay:   own balances: " + ", ".join(own_parts))
    if transfers:
        moved = ", ".join(f"{c1:,} -> {p}" for p, c1 in sorted(transfers.items()))
        left = balances.get("capital_one", 0) - option.affordability.capital_one_used
        lines.append(f"       transfer from Capital One: {moved} ({left:,} left)")
    return lines


def _miss_text(option: TripOption) -> str:
    a = option.affordability
    parts = [f"{short:,} short in {program}" for program, short in sorted(a.shortfalls.items())]
    if a.capital_one_short:
        parts.append(f"{a.capital_one_short:,} short in Capital One")
    if a.unknown_sources:
        parts.append("unmapped program: " + ", ".join(a.unknown_sources))
    return "; ".join(parts)


def render_trip(
    trip: sqlite3.Row,
    gateways: list[str],
    ranked: list[TripOption],
    baseline: TripBaseline | None,
    balances: dict[str, int],
    *,
    cash_note: str | None = None,
    top_n: int = 5,
    near_n: int = 3,
    today: date | None = None,
) -> str:
    today = today or date.today()
    depart = date.fromisoformat(trip["depart_date"])
    dates = f"{depart:%b %d %Y}"
    if trip["return_date"]:
        dates += f" - {date.fromisoformat(trip['return_date']):%b %d %Y}"
    cabin = trip["cabin"].replace("_", " ")

    out = [RULE, f"{trip['name']}   {trip['origin']} > {trip['destination']}   {dates}   ({cabin})"]
    out.append("Via gateways: " + (", ".join(gateways) if gateways else "none (direct pairs only)"))
    out.append(RULE)

    out.append("")
    out.append(
        f"CASH BASELINE  (cheapest fare, layovers <= {DEFAULT_MAX_LAYOVER_MINUTES // 60}h, "
        f"total <= {DEFAULT_MAX_TOTAL_MINUTES // 60}h)"
    )
    if baseline is None:
        out.append(f"  {cash_note or 'Cash prices unavailable.'}")
    else:
        directions = ["out", "ret"][: len(baseline.cabin_fares)]
        for label, fares, total in (
            (cabin, baseline.cabin_fares, baseline.cabin_total),
            ("economy", baseline.economy_fares, baseline.economy_total),
        ):
            legs = "   ".join(f"{d} {_fare_brief(f)}" for d, f in zip(directions, fares))
            out.append(f"  {label:<9}{legs}" + (f"   TOTAL ${total:,}" if total is not None else ""))
        if baseline.cabin_total is not None and baseline.economy_total is not None:
            out.append(f"  {cabin} costs ${baseline.cabin_total - baseline.economy_total:,} more than economy")

    affordable = [o for o in ranked if o.affordability.affordable]
    misses = [o for o in ranked if not o.affordability.affordable]

    out.append("")
    out.append(f"BEST AFFORDABLE OPTIONS  ({len(affordable):,} of {len(ranked):,} combinations)")
    note = _preference_note(affordable)
    if note:
        out.append(f"  {note}")
    if not ranked:
        out.append("  No award options found through the gateways.")
    elif not affordable:
        out.append("  Nothing affordable with current balances. See the closest misses below.")
    for rank, (option, variants) in enumerate(_group_by_shape(affordable)[:top_n], start=1):
        head = f"#{rank}  {option.total_miles:,} miles"
        if baseline is not None:
            value = estimate_value(option, baseline)
            if value.cents_per_mile_vs_cabin is not None:
                head += f"   {value.cents_per_mile_vs_cabin:.2f} c/mile vs {cabin} cash"
            if value.cents_per_mile_vs_economy is not None:
                head += f"   {value.cents_per_mile_vs_economy:.2f} c/mile vs economy cash"
        out.append("")
        out.append(head)
        for label, itinerary in zip(("out ", "back"), option.itineraries):
            out.append(f"  {label}  {_itinerary_text(itinerary)}")
        out.append("  " + _tickets_line(option))
        out.extend("  " + line for line in _payment_lines(option, balances))
        for index, warning in enumerate(_warnings(option, today)):
            out.append(("  check: " if index == 0 else "         ") + warning)
        if variants:
            out.append(f"  (+{variants} other date combination{'s' if variants > 1 else ''} at the same price)")

    if misses:
        out.append("")
        out.append("CLOSEST MISSES  (ordered by how little they're short, not by price)")
        for option, _ in _group_by_shape(misses)[:near_n]:
            out.append(f"  {option.total_miles:,} miles   {_miss_text(option)}")
            for label, itinerary in zip(("out ", "back"), option.itineraries):
                out.append(f"      {label}  {_itinerary_text(itinerary)}")
    return "\n".join(out)


def build_report(
    conn: sqlite3.Connection,
    awards: SeatsAeroClient,
    cash: SerpApiClient | None,
    *,
    top_n: int = 5,
    near_n: int = 3,
    today: date | None = None,
) -> str:
    """Search and report every active trip (and only active trips)."""
    trips = db.active_trips(conn)
    if not trips:
        return "No active trips. Mark a trip active to include it in the report."

    balances = db.get_balances(conn)
    sections = []
    for trip in trips:
        gateways = db.get_gateways(conn, trip["id"])
        ranked = rank_trip(search_trip(awards, trip, gateways), balances)

        baseline, note = None, None
        if cash is None:
            note = "Cash prices skipped: SERPAPI_API_KEY is not set."
        else:
            try:
                baseline = trip_baseline(conn, cash, trip)
            except SerpApiError as exc:
                note = f"Cash prices unavailable: {exc}"

        sections.append(
            render_trip(trip, gateways, ranked, baseline, balances,
                        cash_note=note, top_n=top_n, near_n=near_n, today=today)
        )

    usage = []
    if awards.calls_remaining is not None:
        usage.append(f"Seats.aero calls left today: {awards.calls_remaining}")
    if cash is not None:
        usage.append(f"SerpApi searches this run: {cash.searches_made} (cached results are free)")
    sections.append(
        "NOTES\n"
        "  * = connecting flight. Award figures are before taxes and fees, so cents per mile are upper bounds.\n"
        "  Legs reporting 0 seats are excluded as unbookable (some programs report 0 for everything).\n"
        "  One ticket = same program and same-day connection (assumed, so verify: a program may price the\n"
        "  combined trip differently from its legs). Cash baselines are single tickets, so mixed-program\n"
        "  awards are not a like-for-like comparison on protection.\n"
        "  Seats.aero data is a cached snapshot and shows dates but not flight times. Verify on the\n"
        "  airline's site before transferring Capital One miles, because transfers cannot be undone.\n"
        + ("  " + " | ".join(usage) if usage else "")
    )
    return "\n\n".join(sections)
