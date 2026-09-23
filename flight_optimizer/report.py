"""Plain-text report: ranked award options for each active trip, valued against cash."""

import sqlite3
from datetime import date

from flight_optimizer import db
from flight_optimizer.affordability import SOURCE_TO_PROGRAM, TripOption, slow_transfers
from flight_optimizer.analysis import TripAnalysis, analyze_trip_cabins
from flight_optimizer.cabins import (
    MILE_VALUE_CENTS, UPGRADE_MAX_MULTIPLE, CabinComparison, cabin_label, compare_cabins,
)
from flight_optimizer.cashtrend import CashTrend
from flight_optimizer.buying import ESTIMATE_NOTE, BuyPlan, Purchase, rank_buy_plans
from flight_optimizer.cash_fares import (
    DEFAULT_MAX_CACHE_AGE,
    DEFAULT_MAX_LAYOVER_MINUTES,
    DEFAULT_MAX_TOTAL_MINUTES,
    CashFare,
    SerpApiClient,
)
from flight_optimizer.connections import COMFORTABLE_CONNECTION_MINUTES, MIN_CONNECTION_MINUTES, ConnectionClass
from flight_optimizer.enrichment import EnrichmentStats
from flight_optimizer.joint import JOINT_TOP, JointView, build_joint
from flight_optimizer.itineraries import (
    CURRENT_AWARD_DAYS, STALE_AWARD_DAYS, Freshness, Itinerary, is_stale,
)
from flight_optimizer.promos import Promo, active_promos, best_promos
from flight_optimizer.savings import TripBaseline, estimate_value
from flight_optimizer.taxes import option_taxes, rates_note
from flight_optimizer.seats_aero import AwardResult, Cabin, SeatsAeroClient
from flight_optimizer.windows import EARLY_DEPARTURE_DAYS, option_dates, window_note

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


def _leg_text(leg: AwardResult) -> str:
    when = f"{leg.date:%b %d}"
    if leg.departs_at and leg.arrives_at:
        next_day = "+1" if leg.arrives_at.date() > leg.departs_at.date() else ""
        when += f" {leg.departs_at:%H:%M}-{leg.arrives_at:%H:%M}{next_day}"
    flights = f" {leg.flight_numbers}" if leg.flight_numbers else ""
    return (
        f"{leg.origin}>{leg.destination} {when} {leg.miles:>6,} "
        f"{leg.source}{'' if leg.direct else '*'}{flights}"
    )


def _itinerary_text(itinerary: Itinerary) -> str:
    text = "  +  ".join(_leg_text(leg) for leg in itinerary.legs)
    if itinerary.connection_minutes is not None:
        text += f"   [{_hm(itinerary.connection_minutes)} at {itinerary.via}]"
    return text


def freshness_label(option: TripOption, today: date) -> str:
    """How current an option's data is, by its stalest leg."""
    age = option.oldest_age_days(today)
    oldest = f"oldest {age}d" if age is not None else "age unknown"
    return {
        Freshness.CURRENT: f"data current, {oldest}",
        Freshness.AGING: f"data aging, {oldest}",
        Freshness.STALE: f"DATA STALE, {oldest}",
    }[option.freshness(today)]


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


def _group_by_shape(options: list[TripOption]) -> list[tuple[TripOption, list[TripOption]]]:
    """Ranked options with same-shape date variants folded into (first, the other variants)."""
    groups: dict[tuple, list[TripOption]] = {}
    for option in options:
        groups.setdefault(_shape(option), []).append(option)
    return [(members[0], members[1:]) for members in groups.values()]


def _option_dates(option: TripOption) -> str:
    """The travel dates of an option, e.g. "Jan 07 - Jan 14" (the day it lands, and the day the return leaves)."""
    return option_dates(option)


def _variants_text(option: TripOption, variants: list[TripOption]) -> str:
    """"(+2 other date combinations at the same price: Jan 06 - Jan 13, ...)". The dates are listed only
    when they differ from the shown option's: with flexible dates that is the point of the line."""
    n = len(variants)
    others = list(dict.fromkeys(d for v in variants if (d := _option_dates(v)) != _option_dates(option)))
    listed = ""
    if others:
        listed = ": " + ", ".join(others[:5]) + (f", +{len(others) - 5} more" if len(others) > 5 else "")
    return f"(+{n} other date combination{'s' if n > 1 else ''} at the same price{listed})"


def _leg_name(leg: AwardResult) -> str:
    return f"{leg.origin}>{leg.destination} {leg.source}"


def _warnings(option: TripOption, today: date) -> list[str]:
    """One line per concern, naming the legs involved."""
    itineraries = option.itineraries
    legs = [leg for it in itineraries for leg in it.legs]
    warnings = []

    connecting = [
        _leg_name(leg) + (f" ({leg.stops} stop{'s' if leg.stops != 1 else ''})" if leg.stops else "")
        for leg in legs
        if not leg.direct
    ]
    if connecting:
        warnings.append("connecting flights (*), check the routing: " + ", ".join(connecting))

    unchecked, tight, overnight = [], [], []
    for it in itineraries:
        if len(it.legs) != 2:
            continue
        hub = it.via or it.legs[0].destination
        connection = it.connection_class
        if connection is None:
            if it.legs[0].date == it.legs[1].date:
                unchecked.append(f"{hub} on {it.legs[0].date:%b %d}")
        elif connection is ConnectionClass.TIGHT:
            tight.append(f"{hub} {_hm(it.connection_minutes)}")
        elif connection is ConnectionClass.OVERNIGHT:
            overnight.append(f"{hub} ({_hm(it.connection_minutes)})")
    if unchecked:
        warnings.append("same-day connection at " + ", ".join(unchecked) + " (flight times not checked)")
    if tight:
        warnings.append(
            "tight connection at " + ", ".join(tight)
            + f" (over {MIN_CONNECTION_MINUTES // 60} hour but under "
            f"{COMFORTABLE_CONNECTION_MINUTES / 60:g} hours between flights)"
        )
    if overnight:
        warnings.append("overnight stop at " + ", ".join(overnight) + ": you arrive later than a same-day connection")

    no_seats = [_leg_name(leg) for leg in legs if leg.remaining_seats == 0]
    if no_seats:
        warnings.append("0 seats reported: " + ", ".join(no_seats))

    stale = [f"{_leg_name(leg)} ({leg.updated_at[:10]})" for leg in legs if is_stale(leg, today)]
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
        for program, wait in sorted(slow_transfers(option).items()):
            lines.append(f"       slow transfer: {program} takes {wait.split(',')[0]}")
    return lines


def miss_text(option: TripOption) -> str:
    a = option.affordability
    parts = [f"{short:,} short in {program}" for program, short in sorted(a.shortfalls.items())]
    if a.capital_one_short:
        parts.append(f"{a.capital_one_short:,} short in Capital One")
    if a.unknown_sources:
        parts.append("unmapped program: " + ", ".join(a.unknown_sources))
    return "; ".join(parts)


def _stats_line(stats: EnrichmentStats | None) -> str | None:
    if stats is None or not (stats.lookups or stats.cache_hits or stats.dropped_unsafe or stats.unverifiable
                             or stats.wrong_landing_day):
        return None
    reused = f", {stats.cache_hits} reused from cache" if stats.cache_hits else ""
    parts = [f"Flight times checked for the leading options ({stats.lookups} lookups{reused})"]
    if stats.dropped_unsafe:
        parts.append(f"{stats.dropped_unsafe} dropped for a connection of {MIN_CONNECTION_MINUTES // 60} hour or less")
    if stats.unverifiable:
        parts.append(f"{stats.unverifiable} skipped for lack of usable flight details")
    if stats.wrong_landing_day:
        parts.append(f"{stats.wrong_landing_day} outbound dropped for landing on a day you did not ask for, or over a day after leaving")
    if stats.budget_reached:
        parts.append("stopped early to protect the API quota")
    return "; ".join(parts) + "."


def _booking_count(option: TripOption) -> int:
    return sum(len(it.legs) if it.self_transfers else 1 for it in option.itineraries)


def _label_width(cabin: Cabin) -> int:
    """Column width for a cabin name plus a space, so "premium economy" does not run into the fares."""
    return max(9, len(cabin_label(cabin)) + 1)


def _cash_ticket_lines(
    baseline: TripBaseline | None, cabin: Cabin, best_award: TripOption | None
) -> list[str]:
    """The protected alternative: a cash ticket is one booking each way."""
    if baseline is None or baseline.cabin_total is None:
        return []
    directions = ["out", "ret"][: len(baseline.cabin_fares)]
    fares = "   ".join(f"{d} {_fare_brief(f)}" for d, f in zip(directions, baseline.cabin_fares))
    lines = [
        "",
        "CASH THROUGH-TICKET  (one booking each way: bags checked through, airline covers missed connections)",
        f"  {cabin_label(cabin):<{_label_width(cabin)}}{fares}   TOTAL ${baseline.cabin_total:,}",
    ]
    short = [
        f"{d} {_hm(m)}"
        for d, fare in zip(directions, baseline.cabin_fares)
        for m in fare.layovers
        if m < COMFORTABLE_CONNECTION_MINUTES
    ]
    if short:
        lines.append(
            f"  check: layover under {COMFORTABLE_CONNECTION_MINUTES / 60:g} hours: " + ", ".join(short)
        )
    if cabin is not Cabin.ECONOMY and baseline.economy_total is not None:
        lines.append(f"  economy   TOTAL ${baseline.economy_total:,}")
    if best_award is not None:
        lines.append(
            f"  vs award #1: {best_award.total_miles:,} miles on {_booking_count(best_award)} separate booking"
            f"{'s' if _booking_count(best_award) != 1 else ''}"
        )
    return lines


def purchase_text(p: Purchase) -> str:
    """One purchase, with the sale it uses (if any) and what retail would have cost."""
    q = p.quote
    text = (
        f"buy {q.points_bought:,} {p.program} miles for about ${q.cost_usd:,.2f} "
        f"({q.effective_cents_per_point:.2f} c each, short {p.shortfall:,})"
    )
    if q.kind != "retail":
        until = f" until {q.promo_ends:%b %d}" if q.promo_ends else ""
        best_case = ", BEST CASE: this sale's tiers are not known" if not q.tiers_known else ""
        text += f" [{q.percent}% {q.kind} sale{until}{best_case}; retail ${p.retail_quote.cost_usd:,.2f}]"
    if q.confidence == "low":
        text += ", low-confidence price"
    return text


def _buy_lines(
    misses: list[TripOption], today: date, limit: int, cash_total: int | None = None,
    promos: dict[str, Promo] | None = None,
) -> list[str]:
    """Near-misses that buying miles would make affordable, with the cost of doing so."""
    plans = rank_buy_plans(misses, today, promos)
    shown: list[BuyPlan] = []
    seen: set = set()
    for plan in plans:  # same-shape date variants add nothing
        shape = _shape(plan.option)
        if shape not in seen:
            seen.add(shape)
            shown.append(plan)
        if len(shown) == limit:
            break
    if not shown:
        return []

    lines = ["", f"GAPS YOU COULD CLOSE BY BUYING MILES  ({ESTIMATE_NOTE})"]
    for plan in shown:
        buys = "; ".join(purchase_text(p) for p in plan.purchases)
        lines.append(
            f"  {plan.option.total_miles:,} miles + ${plan.total_cost:,.2f}: {buys}"
            f"   [{freshness_label(plan.option, today)}]"
            + (f"   (cash ticket: ${cash_total:,})" if cash_total is not None else "")
        )
        for label, itinerary in zip(("out ", "back"), plan.option.itineraries):
            lines.append(f"      {label}  {_itinerary_text(itinerary)}")
    return lines


def _money(amount: float | None) -> str:
    return "-" if amount is None else f"${amount:,.0f}"


def render_comparison(comparison: CabinComparison) -> str:
    """One trip across cabins: what each costs by cash or affordable award, and any worthwhile upgrade."""
    trip = comparison.trip
    out = [RULE, f"{trip['name']}   {trip['origin']} > {trip['destination']}   CABINS AT A GLANCE", RULE, ""]
    out.append(f"  {'cabin':<17}{'cash':>9}   {'affordable award (miles + taxes)':<46}{'cheapest way':>14}")
    cheapest = comparison.cheapest
    upgrades = {u.outlook.cabin: u for u in comparison.upgrades}
    for o in comparison.outlooks:
        if o.award is not None:
            taxes = o.award_taxes
            extra = f" + {taxes.text}" if taxes is not None and (taxes.usd or not taxes.complete) else ""
            award = f"{o.award.total_miles:,} miles{extra} (~{_money(o.award_cost)})"
        else:
            award = "none you can afford" if o.offered else "none found"
        way = "-" if o.best_cost is None else f"{_money(o.best_cost)} {o.best_way}"
        line = f"  {cabin_label(o.cabin):<17}{_money(o.cash):>9}   {award:<46}{way:>14}"
        if cheapest is not None and o.cabin is cheapest.cabin:
            line += "   cheapest"
        elif o.cabin in upgrades:
            u = upgrades[o.cabin]
            line += f"   +${u.extra:,.0f} ({u.multiple:.2f}x): UPGRADE OPPORTUNITY"
        out.append(line)
    if not any(o.offered for o in comparison.outlooks):
        out.append("  Nothing priced in any cabin yet.")
    out.append("")
    out.append(
        f"  Awards are costed at {MILE_VALUE_CENTS:g} cent per mile (assumed: Capital One's travel-eraser rate)"
        " plus their taxes and fees in dollars (approximate exchange rates)."
    )
    out.append(
        f"  A better cabin is flagged when it costs at most {UPGRADE_MAX_MULTIPLE:g}x the cheapest way to fly."
        " Only awards you can afford on non-stale data count."
    )
    out.append("  Miles and status earned by a better cabin are not counted, so real upgrades cost less than shown.")
    if trip["flexible"]:
        out.append(
            "  Flexible dates: awards may be on shifted dates, but cash fares are for your exact dates only "
            "(pricing every day would multiply the SerpApi searches)."
        )
    return "\n".join(out)


def render_trip(
    trip: sqlite3.Row,
    gateways: list[str],
    ranked: list[TripOption],
    baseline: TripBaseline | None,
    balances: dict[str, int],
    *,
    cash_note: str | None = None,
    stats: EnrichmentStats | None = None,
    cabin: Cabin = Cabin.BUSINESS,
    cash_trend: CashTrend | None = None,
    cash_target: int | None = None,
    top_n: int = 5,
    near_n: int = 3,
    buy_n: int = 3,
    promos: dict[str, Promo] | None = None,
    today: date | None = None,
) -> str:
    today = today or date.today()
    dates = f"arrive {date.fromisoformat(trip['depart_date']):%b %d %Y}"
    if trip["return_date"]:
        dates += f", leave {date.fromisoformat(trip['return_date']):%b %d %Y}"
    cabin_name = cabin_label(cabin)

    out = [RULE, f"{trip['name']}   {trip['origin']} > {trip['destination']}   {dates}   ({cabin_name}){window_note(trip)}"]
    out.append("Via gateways: " + (", ".join(gateways) if gateways else "none (direct pairs only)"))
    out.append(RULE)

    out.append("")
    out.append(
        f"CASH BASELINE  (cheapest fare, layovers over {MIN_CONNECTION_MINUTES // 60}h and up to "
        f"{DEFAULT_MAX_LAYOVER_MINUTES // 60}h, total <= {DEFAULT_MAX_TOTAL_MINUTES // 60}h)"
    )
    out.append(
        f"  Cash fares leave on the day you want to arrive; awards may also leave up to {EARLY_DEPARTURE_DAYS} day"
        f"{'s' if EARLY_DEPARTURE_DAYS != 1 else ''} earlier."
    )
    if trip["flexible"]:
        out.append("  Cash fares are for your exact dates. Flexible dates apply to awards only.")
    if baseline is None:
        out.append(f"  {cash_note or 'Cash prices unavailable.'}")
    else:
        directions = ["out", "ret"][: len(baseline.cabin_fares)]
        rows = [(cabin_name, baseline.cabin_fares, baseline.cabin_total)]
        if cabin is not Cabin.ECONOMY:
            rows.append(("economy", baseline.economy_fares, baseline.economy_total))
        for label, fares, total in rows:
            legs = "   ".join(f"{d} {_fare_brief(f)}" for d, f in zip(directions, fares))
            out.append(f"  {label:<{_label_width(cabin)}}{legs}" + (f"   TOTAL ${total:,}" if total is not None else ""))
        if cabin is not Cabin.ECONOMY and baseline.cabin_total is not None and baseline.economy_total is not None:
            out.append(f"  {cabin_name} costs ${baseline.cabin_total - baseline.economy_total:,} more than economy")
        if baseline.fetched_at:
            out.append(f"  fares fetched {baseline.fetched_at[:10]} (refreshed once older than {DEFAULT_MAX_CACHE_AGE.days} days)")
        if cash_trend is not None:
            if cash_trend.count:
                since = f" since {cash_trend.first_seen:%b %d}" if cash_trend.first_seen else ""
                out.append(
                    f"  {cabin_name} cash history: {cash_trend.count} earlier search{'es' if cash_trend.count != 1 else ''}{since}, "
                    f"low ${cash_trend.low:,}, typical ${cash_trend.typical:,}"
                )
            else:
                out.append(f"  {cabin_name} cash history: this is the first search kept, so there is nothing to compare with yet")
        if cash_target is not None:
            state = "reached" if baseline.cabin_total is not None and baseline.cabin_total <= cash_target else "not reached"
            out.append(f"  your {cabin_name} target: ${cash_target:,} ({state})")

    affordable = [o for o in ranked if o.affordability.affordable]
    misses = [o for o in ranked if not o.affordability.affordable]

    out.append("")
    out.append(f"BEST AFFORDABLE OPTIONS  ({len(affordable):,} of {len(ranked):,} combinations)")
    note = _preference_note(affordable)
    if note:
        out.append(f"  {note}")
    stats_line = _stats_line(stats)
    if stats_line:
        out.append(f"  {stats_line}")
    if not ranked:
        out.append("  No award options found through the gateways.")
    elif not affordable:
        out.append("  Nothing affordable with current balances. See the closest misses below.")
    for rank, (option, variants) in enumerate(_group_by_shape(affordable)[:top_n], start=1):
        head = f"#{rank}  {option.total_miles:,} miles"
        taxes = option_taxes(option)
        if taxes.usd or not taxes.complete:
            head += f" + {taxes.text}"
        if trip["flexible"]:
            head += f"   {_option_dates(option)}"
        if baseline is not None:
            value = estimate_value(option, baseline)
            if value.cents_per_mile_vs_cabin is not None:
                head += f"   {value.cents_per_mile_vs_cabin:.2f} c/mile vs {cabin_name} cash"
            if cabin is not Cabin.ECONOMY and value.cents_per_mile_vs_economy is not None:
                head += f"   {value.cents_per_mile_vs_economy:.2f} c/mile vs economy cash"
        head += f"   [{freshness_label(option, today)}]"
        out.append("")
        out.append(head)
        for label, itinerary in zip(("out ", "back"), option.itineraries):
            out.append(f"  {label}  {_itinerary_text(itinerary)}")
        out.append("  " + _tickets_line(option))
        out.extend("  " + line for line in _payment_lines(option, balances))
        for index, warning in enumerate(_warnings(option, today)):
            out.append(("  check: " if index == 0 else "         ") + warning)
        if variants:
            out.append("  " + _variants_text(option, variants))

    out.extend(_cash_ticket_lines(baseline, cabin, affordable[0] if affordable else None))

    if misses:
        out.append("")
        out.append("CLOSEST MISSES  (ordered by how little they're short, not by price)")
        for option, _ in _group_by_shape(misses)[:near_n]:
            out.append(f"  {option.total_miles:,} miles   {miss_text(option)}")
            for label, itinerary in zip(("out ", "back"), option.itineraries):
                out.append(f"      {label}  {_itinerary_text(itinerary)}")
    out.extend(_buy_lines(misses, today, buy_n, baseline.cabin_total if baseline is not None else None, promos))
    return "\n".join(out)


def render_joint(view: JointView) -> str:
    """Trips compared with each other on the shared balances (see `joint.py`)."""
    out = [RULE, "SHARED BALANCES   the trips are checked against each other", RULE, ""]
    for pair in view.pairs:
        first, second = pair.first["name"], pair.second["name"]
        out.append(f"{first} and {second}")
        if not pair.competing:
            who = [t["name"] for t, c in ((pair.first, pair.first_candidates), (pair.second, pair.second_candidates)) if c]
            out.append(
                f"  No competition: only {who[0]} has an affordable award right now." if who
                else "  No competition: neither has an affordable award right now."
            )
            out.append("")
            continue
        if pair.best is not None:
            a, b = pair.best.first, pair.best.second
            out.append(
                f"  Both by award: possible. Cheapest pair: {first} {cabin_label(a.cabin)} {a.option.total_miles:,} miles"
                f" + {second} {cabin_label(b.cabin)} {b.option.total_miles:,} miles = {pair.best.total_miles:,} miles."
            )
            used = pair.best.affordability.capital_one_used
            if used:
                out.append(f"  That pair moves {used:,} Capital One miles in all.")
        else:
            out.append(
                "  Both by award: NOT possible. Each trip has an affordable option, but no pair of them is affordable "
                "together. Choose which trip gets awards, or buy miles for the other."
            )
        for n in pair.narrowings:
            kept = "nothing affordable" if not n.kept else f"only {' and '.join(cabin_label(c) for c in n.kept)} stay affordable"
            out.append(
                f"  {n.candidate.trip['name']} {cabin_label(n.candidate.cabin)} (leading option, "
                f"{n.candidate.option.total_miles:,} miles) leaves {n.other['name']}: {kept}."
            )
        out.append("")
    out.append(
        f"  Checked pair by pair, on the leading affordable options in each cabin (up to {JOINT_TOP} each) whose data is not "
        "stale and whose miles beat cash."
    )
    return "\n".join(out)


def render_trip_sections(
    analyses: list[TripAnalysis],
    balances: dict[str, int],
    *,
    promos: dict[str, Promo] | None = None,
    today: date | None = None,
    top_n: int = 5,
    near_n: int = 3,
    buy_n: int = 3,
) -> list[str]:
    """One trip's report text: the cabin comparison, then a section per cabin. `analyses` are that
    trip's per-cabin analyses."""
    sections = [render_comparison(compare_cabins(analyses, today or date.today()))]
    for analysis in analyses:
        sections.append(
            render_trip(
                analysis.trip, analysis.gateways, analysis.ranked, analysis.baseline, balances,
                cash_note=analysis.cash_note, stats=analysis.stats, cabin=analysis.cabin,
                cash_trend=analysis.cash_trend, cash_target=analysis.cash_target,
                top_n=top_n, near_n=near_n, buy_n=buy_n, promos=promos, today=today,
            )
        )
    return sections


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
    live_promos = best_promos(active_promos(conn, today or date.today()))
    sections, groups = [], []
    for trip in trips:
        analyses = analyze_trip_cabins(conn, awards, cash, trip, balances)
        groups.append(analyses)
        sections += render_trip_sections(
            analyses, balances, promos=live_promos, today=today, top_n=top_n, near_n=near_n
        )
    if len(groups) > 1:
        sections.append(render_joint(build_joint(groups, balances, today or date.today())))

    usage = []
    if awards.calls_remaining is not None:
        served = getattr(awards, "cache_hits", 0)
        usage.append(
            f"Seats.aero calls left today: {awards.calls_remaining}"
            + (f" ({served} lookups served from cache)" if served else "")
        )
    if cash is not None:
        usage.append(f"SerpApi searches this run: {cash.searches_made} (cached results are free)")
    sections.append(
        "NOTES\n"
        "  * = connecting flight. Cents per mile are after the award's taxes and fees (cash replaced minus taxes, over miles).\n"
        f"  {rates_note()}\n"
        "  A leg reporting no taxes counts as none, though Seats.aero cannot tell free from unknown.\n"
        "  Legs reporting 0 seats are excluded as unbookable (some programs report 0 for everything).\n"
        "  One ticket = same program and same-day connection (assumed, so verify: a program may price the\n"
        "  combined trip differently from its legs). Cash baselines are single tickets, so mixed-program\n"
        "  awards are not a like-for-like comparison on protection.\n"
        f"  Freshness: award data {CURRENT_AWARD_DAYS} days old or newer is current, up to {STALE_AWARD_DAYS} days is aging, older is\n"
        "  stale. Options rank by their stalest leg first, and alerts never fire on options resting on stale data.\n"
        "  Connections: same day with 2.5+ hours is preferred, under 2.5 hours warns, 1 hour or less is never\n"
        "  recommended. Flight times are local and come from Seats.aero's cached snapshot. Verify on the\n"
        "  airline's site before transferring Capital One miles, because transfers cannot be undone.\n"
        + ("  " + " | ".join(usage) if usage else "")
    )
    return "\n\n".join(sections)
