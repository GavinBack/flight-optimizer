"""The weekly digest: a quiet summary, so silence is never ambiguous.

The daily check alerts only when something is worth acting on, and stays silent when
award data is stale. Silence can then mean "nothing exists" or "nothing is current" or
"nothing changed". The digest says which, once a week. It never includes balance amounts.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from flight_optimizer import db
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.buying import rank_buy_plans
from flight_optimizer.itineraries import STALE_AWARD_DAYS, Freshness
from flight_optimizer.programs import PROGRAM_NAMES, STALE_AFTER_DAYS
from flight_optimizer.promos import Promo
from flight_optimizer.report import miss_text
from flight_optimizer.savings import estimate_value

DIGEST_INTERVAL = timedelta(days=7)
_META_KEY = "digest:last_sent"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Digest:
    title: str
    message: str
    priority: int = 2  # low: a summary, not an alert
    tags: tuple[str, ...] = ("calendar",)
    click: str | None = None


def digest_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if no digest has been sent, or the last one was at least a week ago."""
    last = db.get_meta(conn, _META_KEY)
    if last is None:
        return True
    return now.astimezone(timezone.utc) - datetime.fromisoformat(last) >= DIGEST_INTERVAL


def record_digest_sent(conn: sqlite3.Connection, now: datetime) -> None:
    db.set_meta(conn, _META_KEY, now.astimezone(timezone.utc).isoformat())


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _dates(trip: sqlite3.Row) -> str:
    depart = date.fromisoformat(trip["depart_date"])
    text = f"{depart:%b %d}"
    if trip["return_date"]:
        text += f" - {date.fromisoformat(trip['return_date']):%b %d}"
    return text


def _alert_line(counts: dict[str, int]) -> str:
    award, sale = counts.get("award", 0), counts.get("promo", 0)
    if not award and not sale:
        return "Alerts sent in the last 7 days: none."
    parts = []
    if award:
        parts.append(f"{award} award")
    if sale:
        parts.append(f"{sale} sale")
    return "Alerts sent in the last 7 days: " + ", ".join(parts) + "."


def trip_section(
    analysis: TripAnalysis, alert_counts: dict[str, int], promos: dict[str, Promo], today: date
) -> list[str]:
    trip = analysis.trip
    lines = [f"{trip['name']}: {trip['origin']} > {trip['destination']}  {_dates(trip)}"]

    affordable = analysis.affordable
    if not affordable:
        misses = [o for o in analysis.ranked if not o.affordability.affordable]
        closest = f" Closest: {miss_text(misses[0])}." if misses else ""
        lines.append(f"No affordable award options right now.{closest}")
    else:
        tiers = {tier: sum(o.freshness(today) is tier for o in affordable) for tier in Freshness}
        lines.append(
            f"{len(affordable)} affordable award option{'s' if len(affordable) != 1 else ''}: "
            f"{tiers[Freshness.CURRENT]} current, {tiers[Freshness.AGING]} aging, {tiers[Freshness.STALE]} stale."
        )
        usable = [o for o in affordable if o.freshness(today) is not Freshness.STALE]
        if usable:
            best = usable[0]
            value = ""
            if analysis.baseline is not None:
                estimate = estimate_value(best, analysis.baseline)
                if estimate.cents_per_mile_vs_cabin is not None:
                    value = f" ({estimate.cents_per_mile_vs_cabin:.1f} c/mile vs ${estimate.cabin_cash:,} cash)"
            lines.append(f"Best with usable data: {best.total_miles:,} miles{value}.")
        else:
            freshest = min(
                (age for o in affordable if (age := o.oldest_age_days(today)) is not None), default=None
            )
            age = f" (the freshest is {_days(freshest)} old)" if freshest is not None else ""
            lines.append(
                f"None has data newer than {STALE_AWARD_DAYS} days{age}, so no award alert can fire "
                "until Seats.aero refreshes it. That is silence, not an absence of options."
            )

    lines.append(_alert_line(alert_counts))

    misses = [
        o for o in analysis.ranked
        if not o.affordability.affordable and o.freshness(today) is not Freshness.STALE
    ]
    plans = rank_buy_plans(misses, today, promos)
    if plans:
        plan = plans[0]
        what = ", ".join(f"{p.shortfall:,} {p.program}" for p in plan.purchases)
        sale = ""
        if plan.uses_promo:
            known = all(p.quote.tiers_known for p in plan.purchases if p.quote.kind != "retail")
            sale = " (a sale applies)" if known else " (a sale applies, best case: its tiers are unknown)"
        lines.append(f"Cheapest gap to close by buying: {what} miles, about ${plan.total_cost:,.2f}{sale}.")

    if analysis.baseline is not None and analysis.baseline.fetched_at:
        lines.append(f"Cash fares as of {analysis.baseline.fetched_at[:10]}.")
    return lines


def balance_line(balances: dict[str, int], updated: dict[str, str], today: date) -> str:
    """Whether the balances behind every calculation are current. Never states an amount."""
    held = [p for p, amount in balances.items() if amount > 0 and p in updated]
    if not held:
        return "Balances: none set. Add them with: python -m flight_optimizer balance"
    ages = {
        p: (today - datetime.strptime(updated[p], _TIME_FORMAT).date()).days for p in held
    }
    oldest = max(ages, key=ages.get)
    if ages[oldest] > STALE_AFTER_DAYS:
        return (
            f"Balances: {PROGRAM_NAMES.get(oldest, oldest)} was last updated {_days(ages[oldest])} ago. "
            "Update with: python -m flight_optimizer balance"
        )
    return f"Balances: all updated within {_days(max(ages.values()))}."


def build_digest(
    analyses: list[TripAnalysis],
    *,
    alert_counts: dict[int, dict[str, int]],
    promos: dict[str, Promo],
    balances: dict[str, int],
    balance_updates: dict[str, str],
    today: date,
) -> Digest:
    lines: list[str] = []
    for analysis in analyses:
        if lines:
            lines.append("")
        lines += trip_section(analysis, alert_counts.get(analysis.trip["id"], {}), promos, today)
    lines.append("")
    live = len(promos)
    lines.append(f"Buy-miles sales on record: {live}." if live else "No live buy-miles sales on record.")
    lines.append(balance_line(balances, balance_updates, today))
    lines.append("Full report: python -m flight_optimizer report")

    names = ", ".join(a.trip["name"] for a in analyses)
    title = f"Weekly flight check: {names}" if len(analyses) == 1 else f"Weekly flight check: {len(analyses)} trips"
    return Digest(title=title, message="\n".join(lines))


def alert_counts_for(conn: sqlite3.Connection, analyses: list[TripAnalysis], now: datetime) -> dict[int, dict[str, int]]:
    since = (now.astimezone(timezone.utc) - DIGEST_INTERVAL).strftime(_TIME_FORMAT)
    return {a.trip["id"]: db.count_alerts_since(conn, a.trip["id"], since) for a in analyses}
