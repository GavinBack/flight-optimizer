"""Alert on cash fares: at or under the most the owner would pay, or an economy fare that has really dropped.

Only for cabins the trip's goals cover (`goals.py`). Two rules, both about a cabin's round-trip cash total:

- Max: the trip's goals set a max for the cabin (`trip goal`), and the fare is at or under it.
- Drop (economy only, automatic, and only when economy has no max): the fare is a new low for the last
  30 days AND at least `DROP_PERCENT` and `DROP_DOLLARS` under the typical (median) fare, judged over at
  least `MIN_HISTORY` earlier searches. With a max set, being at or under it is the news instead, so a
  drop above the max says nothing the owner asked for. A better cabin's price is watched by the
  better-cabin alert (`alerts.decide_upgrade_alert`) or by its max.

The fare is for the trip's exact dates even if the trip is flexible (see `windows.py`). A cash alert is
not repeated within `alerts.RE_ALERT_DAYS` unless the fare got lower. It never includes balances.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flight_optimizer import db
from flight_optimizer.alerts import RE_ALERT_DAYS, _dates, _parse_time
from flight_optimizer.analysis import TripAnalysis
from flight_optimizer.cabins import cabin_label
from flight_optimizer.seats_aero import Cabin
from flight_optimizer.windows import any_flexible

DROP_PERCENT = 10
DROP_DOLLARS = 50
MIN_HISTORY = 3
AUTO_DROP_CABINS = (Cabin.ECONOMY,)


@dataclass(frozen=True)
class CashAlert:
    trip_id: int
    cabin: Cabin
    kind: str  # "max" or "drop"
    price: int  # the round-trip total
    key: str
    title: str
    message: str
    click: str | None
    priority: int
    tags: tuple[str, ...]


def is_drop(analysis: TripAnalysis) -> bool:
    """A new 30-day low, well under the typical fare, on enough history to mean something."""
    trend = analysis.cash_trend
    if analysis.cabin not in AUTO_DROP_CABINS or analysis.cabin_max is not None:
        return False
    if trend is None or trend.count < MIN_HISTORY:
        return False
    return (
        trend.current < trend.low
        and trend.current <= trend.typical * (1 - DROP_PERCENT / 100)
        and trend.typical - trend.current >= DROP_DOLLARS
    )


def at_max(analysis: TripAnalysis) -> bool:
    total = analysis.baseline.cabin_total if analysis.baseline is not None else None
    return total is not None and analysis.cabin_max is not None and total <= analysis.cabin_max


def build_cash_alert(analysis: TripAnalysis, kind: str) -> CashAlert:
    trip, cabin = analysis.trip, cabin_label(analysis.cabin)
    baseline, trend = analysis.baseline, analysis.cash_trend
    total = baseline.cabin_total
    fares = ", ".join(f"{d} ${f.price:,}" for d, f in zip(("out", "back"), baseline.cabin_fares))
    lines = [
        f"{trip['origin']} > {trip['destination']}  {_dates(trip)}  ({cabin} cash)",
        f"${total:,} round trip ({fares})" if trip["return_date"] else f"${total:,} one way",
    ]
    if kind == "max":
        lines.append(f"At or under your ${analysis.cabin_max:,} {cabin} max.")
    if trend is not None and trend.count:
        since = f" since {trend.first_seen:%b %d}" if trend.first_seen else ""
        lines.append(
            f"Typical lately: ${trend.typical:,} (median of {trend.count} search{'es' if trend.count != 1 else ''}{since}); "
            f"lowest before: ${trend.low:,}."
        )
    if any_flexible(trip):
        lines.append("This fare is for your exact dates; flexible dates apply to awards only.")
    if baseline.cabin_fetched_at:
        lines.append(f"Cash fares as of {baseline.cabin_fetched_at[:10]}. Fares change fast: verify before booking.")
    lines.append("Details: python -m flight_optimizer report")
    headline = f"within your ${analysis.cabin_max:,} max" if kind == "max" else "dropped"
    return CashAlert(
        trip_id=trip["id"], cabin=analysis.cabin, kind=kind, price=total, key=f"cash:{analysis.cabin.name}:{kind}",
        title=f"{trip['name']}: {cabin} cash fare {headline}: ${total:,}",
        message="\n".join(lines), click=None, priority=3, tags=("chart_with_downwards_trend",),
    )


def decide_cash_alert(
    conn: sqlite3.Connection, analysis: TripAnalysis, *, now: datetime | None = None
) -> CashAlert | None:
    """An alert for this cabin's cash fare, or None: nothing notable, or the same news was sent recently."""
    if analysis.baseline is None or analysis.baseline.cabin_total is None or not analysis.goals.covers(analysis.cabin):
        return None
    kind = "max" if at_max(analysis) else "drop" if is_drop(analysis) else None
    if kind is None:
        return None
    alert = build_cash_alert(analysis, kind)

    last = db.last_alert(conn, alert.trip_id, alert.key)
    if last is not None:
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        recent = when - _parse_time(last["created_at"]) < timedelta(days=RE_ALERT_DAYS)
        cheaper = last["cash_price"] is not None and alert.price < last["cash_price"]
        if recent and not cheaper:
            return None
    return alert


def record_cash_alert_sent(conn: sqlite3.Connection, alert: CashAlert) -> None:
    db.record_alert(
        conn, trip_id=alert.trip_id, alert_type="cash", dedupe_key=alert.key, cash_price=alert.price,
    )
