"""Command line.

    python -m flight_optimizer [report] [--top N]   report on every active trip
    python -m flight_optimizer notify [--dry-run]   alert (via ntfy) on anything new
    python -m flight_optimizer plan [TRIP] [--refresh]   show the booking plan
    python -m flight_optimizer advise [TRIP] [--dry-run]   ask Claude to weigh the options (paid API)
    python -m flight_optimizer done TRIP            mark a trip booked (stops alerts)
    python -m flight_optimizer test-notification    check that ntfy reaches your phone
    python -m flight_optimizer trip [list|add|set]   see, add or change trips
    python -m flight_optimizer balance [PROGRAM MILES ...]   show balances, or set them
    python -m flight_optimizer gateways [TRIP] [--set|--add|--remove AIRPORT ...]   show or change hubs
    python -m flight_optimizer digest [--dry-run]   send (or preview) the weekly digest now
    python -m flight_optimizer promos [--no-fetch]   list buy-miles sales (reads the feeds first)
    python -m flight_optimizer promos add PROGRAM --bonus N --ends YYYY-MM-DD [--tier ...]   record one by hand

Add --fresh to any command to skip the 1-hour search cache.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from flight_optimizer import db
from flight_optimizer.advisor import SYSTEM_PROMPT, AdvisorError, AnthropicClient, build_brief, estimate_tokens
from flight_optimizer.alerts import (
    assess_urgency, decide_alert, decide_promo_alert, decide_upgrade_alert, record_promo_sent, record_sent,
    record_upgrade_sent, usable_for_alert,
)
from flight_optimizer.analysis import analyze_trip_cabins
from flight_optimizer.cabins import MILE_VALUE_CENTS, cabin_label
from flight_optimizer.cash_fares import SerpApiClient
from flight_optimizer.config import ConfigError, Settings, load_settings
from flight_optimizer.digest import alert_counts_for, build_digest, digest_due, record_digest_sent
from flight_optimizer.itineraries import EVALUATED_CABINS, STALE_AWARD_DAYS, Freshness
from flight_optimizer.notify import NtfyClient, NtfyError
from flight_optimizer.plans import latest_plan, render_plan, save_plan
from flight_optimizer.seats_aero import Cabin
from flight_optimizer.promos import (
    Promo, PromoSource, PromoTier, active_promos, best_promos, refresh_promos, save_promo,
)
from flight_optimizer.buying import RETAIL_PRICES
from flight_optimizer.programs import PROGRAM_NAMES, format_balances, parse_miles, resolve_program
from flight_optimizer.trips import add_trip, format_trips, parse_date
from flight_optimizer.report import build_report
from flight_optimizer.seats_aero import SeatsAeroClient, SeatsAeroError
from flight_optimizer.seats_cache import CachedSeatsAero


@dataclass
class Clients:
    awards: SeatsAeroClient
    cash: SerpApiClient | None
    ntfy: NtfyClient | None
    promos: PromoSource | None = None  # None: never go online for sales (tests, or by choice)
    advisor: AnthropicClient | None = None  # None: no ANTHROPIC_API_KEY, so no `advise`


def default_clients(settings: Settings, conn, fresh: bool = False) -> Clients:
    """Real clients. Seats.aero calls go through a local cache; `fresh` skips the
    short-lived search cache (flight details Seats.aero hasn't changed are still reused)."""
    return Clients(
        awards=CachedSeatsAero(
            SeatsAeroClient(settings.seats_aero_api_key), conn, use_search_cache=not fresh
        ),
        cash=SerpApiClient(settings.serpapi_api_key) if settings.serpapi_api_key else None,
        ntfy=NtfyClient(settings.ntfy_topic, server=settings.ntfy_server) if settings.ntfy_topic else None,
        promos=PromoSource(),
        advisor=(
            AnthropicClient(settings.anthropic_api_key, model=settings.anthropic_model)
            if settings.anthropic_api_key else None
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flight_optimizer",
        description="Find and alert on affordable award options for each active trip.",
    )
    parser.add_argument("--top", type=int, default=5, help="award options to show per trip (default 5)")
    parser.add_argument(
        "--fresh", action="store_true",
        help="always run new Seats.aero searches instead of reusing ones under an hour old",
    )
    commands = parser.add_subparsers(dest="command")

    report = commands.add_parser("report", help="print the report (the default)")
    report.add_argument("--top", type=int, default=argparse.SUPPRESS)

    notify = commands.add_parser("notify", help="send an ntfy alert for anything new")
    notify.add_argument("--dry-run", action="store_true", help="print what would be sent; send and save nothing")

    plan = commands.add_parser("plan", help="show the saved booking plan for a trip")
    plan.add_argument("trip_id", nargs="?", type=int)
    plan.add_argument("--refresh", action="store_true", help="search again and rebuild the plan first")
    plan.add_argument("--cabin", choices=[c.name.lower() for c in EVALUATED_CABINS],
                      help="only this cabin (default: the latest saved plan, or with --refresh one per cabin)")

    advise = commands.add_parser(
        "advise", help="ask Claude to weigh the options for your trips (uses the paid Claude API)"
    )
    advise.add_argument("trip_id", nargs="?", type=int, help="omit to advise on every active trip in one call")
    advise.add_argument("--dry-run", action="store_true", help="print exactly what would be sent; send nothing, spend nothing")

    done = commands.add_parser("done", help="mark a trip booked, so it stops being searched and alerted on")
    done.add_argument("trip_id", type=int)

    commands.add_parser("test-notification", help="send a test message to your ntfy topic")

    digest = commands.add_parser("digest", help="send the weekly digest now (it is also sent by notify once a week)")
    digest.add_argument("--dry-run", action="store_true", help="print the digest; send nothing and keep the weekly timer as is")

    promos = commands.add_parser("promos", help="list buy-miles sales, refreshing them first")
    promos.add_argument("--no-fetch", action="store_true", help="list what is stored without going online")
    actions = promos.add_subparsers(dest="promo_action")
    add = actions.add_parser("add", help="record a sale you saw on a program's own site")
    add.add_argument("program")
    kind = add.add_mutually_exclusive_group(required=True)
    kind.add_argument("--bonus", type=int, metavar="PERCENT", help="extra points on top of what you buy")
    kind.add_argument("--discount", type=int, metavar="PERCENT", help="a lower price for the same points")
    add.add_argument("--ends", required=True, metavar="YYYY-MM-DD")
    add.add_argument("--tier", action="append", default=[], metavar="MIN-MAX:PERCENT",
                     help="a purchase tier, e.g. 5000-25000:50 or 80000+:100 (repeatable). Without tiers the "
                          "sale is a best case and never alerts")
    add.add_argument("--url", default="", help="the sale's page, opened when you tap an alert")
    remove = actions.add_parser("remove", help="remove the sales you added by hand for a program")
    remove.add_argument("program")

    trip = commands.add_parser("trip", help="list, add, or change trips")
    trip_actions = trip.add_subparsers(dest="trip_action")
    trip_actions.add_parser("list", help="show every trip (the default)")
    add_trip_parser = trip_actions.add_parser("add", help="add a trip; it is evaluated by the next report and daily check")
    add_trip_parser.add_argument("name")
    add_trip_parser.add_argument("origin", help="airport code, e.g. RNO")
    add_trip_parser.add_argument("destination", help="airport code, e.g. YLW")
    add_trip_parser.add_argument("--depart", required=True, metavar="YYYY-MM-DD")
    add_trip_parser.add_argument("--return", dest="return_date", metavar="YYYY-MM-DD", help="omit for a one-way trip")
    add_trip_parser.add_argument("--gateways", nargs="+", default=[], metavar="AIRPORT",
                                 help="hub airports to route through when origin and destination have no direct award route")
    add_trip_parser.add_argument("--inactive", action="store_true", help="save it without searching or alerting on it yet")
    set_trip = trip_actions.add_parser("set", help="change a trip's name, dates, or whether it is active")
    set_trip.add_argument("trip_id", type=int)
    set_trip.add_argument("--name")
    set_trip.add_argument("--depart", metavar="YYYY-MM-DD")
    set_trip.add_argument("--return", dest="return_date", metavar="YYYY-MM-DD")
    set_trip.add_argument("--one-way", action="store_true", help="drop the return leg")
    state = set_trip.add_mutually_exclusive_group()
    state.add_argument("--active", action="store_true", help="search and alert on this trip")
    state.add_argument("--inactive", action="store_true", help="stop searching and alerting on this trip")

    balance = commands.add_parser(
        "balance", help="show your balances, or set them: balance PROGRAM MILES [PROGRAM MILES ...]"
    )
    balance.add_argument("pairs", nargs="*", metavar="PROGRAM MILES")

    gateways = commands.add_parser("gateways", help="show or change the hub airports a trip is searched through")
    gateways.add_argument("trip_id", nargs="?", type=int, help="omit if only one trip is active")
    change = gateways.add_mutually_exclusive_group()
    change.add_argument("--set", nargs="+", metavar="AIRPORT", help="replace the list")
    change.add_argument("--add", nargs="+", metavar="AIRPORT", help="add airports")
    change.add_argument("--remove", nargs="+", metavar="AIRPORT", help="remove airports")
    return parser


def _pick_trip(conn, trip_id: int | None):
    if trip_id is not None:
        trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if trip is None:
            raise LookupError(f"No trip with id {trip_id}")
        return trip
    active = db.active_trips(conn)
    if len(active) == 1:
        return active[0]
    names = ", ".join(f"{t['id']} ({t['name']})" for t in active) or "none active"
    raise LookupError(f"Say which trip: {names}")


def _send(conn, clients: Clients, trip, alert, *, dry_run: bool, describe: str) -> bool:
    """Print (dry run) or deliver one alert. False if it could not be delivered."""
    if dry_run:
        print(f"--- would send for {trip['name']}: {describe} (priority {alert.priority}) ---")
        print(alert.title)
        print(alert.message)
        return True
    try:
        clients.ntfy.send(alert.message, title=alert.title, priority=alert.priority, tags=alert.tags, click=alert.click)
    except NtfyError as exc:
        print(f"{trip['name']}: could not send the {describe}: {exc}", file=sys.stderr)
        return False  # not recorded, so the next run tries again
    return True


def _run_notify(conn, clients: Clients, plans_dir: Path, *, dry_run: bool, now: datetime) -> int:
    trips = db.active_trips(conn)
    if not trips:
        print("No active trips. Mark a trip active to be alerted about it.")
        return 0
    if not dry_run and clients.ntfy is None:
        print("error: NTFY_TOPIC is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1

    if clients.promos is not None:
        refreshed = refresh_promos(conn, clients.promos, now)
        for problem in refreshed.errors:
            print(f"note: could not read a sales source: {problem}", file=sys.stderr)
    live_promos = best_promos(active_promos(conn, now.date()))

    balances = db.get_balances(conn)
    failed = False
    analyses = []
    for trip in trips:
        trip_analyses = analyze_trip_cabins(conn, clients.awards, clients.cash, trip, balances)
        analyses += trip_analyses

        for analysis in trip_analyses:
            failed |= _alerts_for(
                conn, clients, plans_dir, analysis, live_promos, balances, now, dry_run=dry_run
            )

        upgrade_alert = decide_upgrade_alert(conn, trip_analyses, now=now)
        if upgrade_alert is not None:
            if _send(conn, clients, trip, upgrade_alert, dry_run=dry_run, describe="better-cabin alert"):
                if not dry_run:
                    record_upgrade_sent(conn, upgrade_alert)
                    print(f"{trip['name']}: better-cabin alert sent ({upgrade_alert.title}).")
            else:
                failed = True

    if digest_due(conn, now) and not _send_digest(
        conn, clients, analyses, live_promos, balances, now, dry_run=dry_run
    ):
        failed = True
    return 1 if failed else 0


def _alerts_for(conn, clients: Clients, plans_dir: Path, analysis, live_promos, balances, now: datetime, *, dry_run: bool) -> bool:
    """Award and sale alerts for one trip in one cabin. True if any could not be delivered."""
    trip, failed = analysis.trip, False
    label = f"{trip['name']} ({cabin_label(analysis.cabin)})"

    alert = decide_alert(conn, analysis, now=now)
    if alert is None:
        current = [o for o in analysis.affordable if o.freshness(now.date()) is not Freshness.STALE]
        if analysis.affordable and not current:
            print(
                f"{label}: affordable options exist, but every one relies on award data older "
                f"than {STALE_AWARD_DAYS} days, so no alert was sent. Run the report to see them."
            )
        elif current and not usable_for_alert(analysis, now.date()):
            print(
                f"{label}: affordable options exist, but their miles are worth less than the cash fare "
                f"(under {MILE_VALUE_CENTS:g} cent a mile), so no alert was sent. Run the report to see them."
            )
        else:
            print(f"{label}: nothing new to alert on.")
    elif dry_run:
        _send(conn, clients, trip, alert, dry_run=True, describe="award alert")
    else:
        # Save the plan first, so the alert's "full steps" pointer is always valid.
        plan_text = render_plan(analysis, alert.option, balances=balances, generated_at=now, urgency=alert.urgency)
        path = save_plan(plan_text, trip, generated_at=now, cabin=analysis.cabin, directory=plans_dir)
        if _send(conn, clients, trip, alert, dry_run=False, describe="award alert"):
            record_sent(conn, alert, str(path))
            print(f"{label}: alert sent ({alert.urgency.label}). Plan saved to {path}")
        else:
            failed = True

    promo_alert = decide_promo_alert(conn, analysis, live_promos, now=now)
    if promo_alert is not None:
        if _send(conn, clients, trip, promo_alert, dry_run=dry_run, describe="sale alert"):
            if not dry_run:
                record_promo_sent(conn, promo_alert)
                print(f"{label}: sale alert sent ({promo_alert.title}).")
        else:
            failed = True
    return failed


def _send_digest(conn, clients: Clients, analyses, live_promos, balances, now: datetime, *, dry_run: bool) -> bool:
    """Build and deliver the weekly digest. The weekly timer only restarts once it was really sent."""
    digest = build_digest(
        analyses,
        alert_counts=alert_counts_for(conn, analyses, now),  # after this run's alerts, so they are counted
        promos=live_promos,
        balances=balances,
        balance_updates=db.balance_updates(conn),
        today=now.date(),
    )
    if not _send(conn, clients, {"name": "all trips"}, digest, dry_run=dry_run, describe="weekly digest"):
        return False
    if not dry_run:
        record_digest_sent(conn, now)
        print("Weekly digest sent.")
    return True


def _run_digest(conn, clients: Clients, now: datetime, *, dry_run: bool) -> int:
    trips = db.active_trips(conn)
    if not trips:
        print("No active trips, so there is nothing to summarize.")
        return 0
    if not dry_run and clients.ntfy is None:
        print("error: NTFY_TOPIC is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    if clients.promos is not None:
        for problem in refresh_promos(conn, clients.promos, now).errors:
            print(f"note: could not read a sales source: {problem}", file=sys.stderr)
    balances = db.get_balances(conn)
    analyses = [a for trip in trips for a in analyze_trip_cabins(conn, clients.awards, clients.cash, trip, balances)]
    live_promos = best_promos(active_promos(conn, now.date()))
    return 0 if _send_digest(conn, clients, analyses, live_promos, balances, now, dry_run=dry_run) else 1


def _run_advise(conn, clients: Clients, args, now: datetime) -> int:
    trips = [_pick_trip(conn, args.trip_id)] if args.trip_id is not None else db.active_trips(conn)
    if not trips:
        print("No active trips, so there is nothing to advise on.")
        return 0
    if not args.dry_run and clients.advisor is None:
        print("error: ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1

    balances = db.get_balances(conn)
    live_promos = best_promos(active_promos(conn, now.date()))
    analyses = [analyze_trip_cabins(conn, clients.awards, clients.cash, trip, balances) for trip in trips]
    brief = build_brief(analyses, balances, promos=live_promos, today=now.date())
    size = estimate_tokens(SYSTEM_PROMPT + brief)

    if args.dry_run:
        print("SYSTEM PROMPT\n" + SYSTEM_PROMPT + "\n\nMESSAGE\n" + brief)
        print(f"\nDry run: about {size:,} input tokens; nothing was sent. Balances are included above.")
        return 0

    print(f"Asking Claude ({clients.advisor.model}) about {len(trips)} trip{'s' if len(trips) != 1 else ''} "
          f"(about {size:,} input tokens)...\n")
    advice = clients.advisor.advise(brief)
    print(advice.text)
    if advice.truncated:
        print("\n[The answer was cut off at the length limit.]")
    print(f"\nAdvice from {advice.model}: {advice.input_tokens:,} tokens in, {advice.output_tokens:,} out. "
          "It is judgment over the computed figures, not a booking instruction: verify before transferring miles.")
    return 0


def _run_plan(conn, clients: Clients, plans_dir: Path, args, now: datetime) -> int:
    trip = _pick_trip(conn, args.trip_id)
    cabin = Cabin[args.cabin.upper()] if args.cabin else None
    if args.refresh:
        balances = db.get_balances(conn)
        analyses = analyze_trip_cabins(
            conn, clients.awards, clients.cash, trip, balances,
            cabins=(cabin,) if cabin else EVALUATED_CABINS,
        )
        for analysis in analyses:
            if not analysis.affordable:
                print(f"{trip['name']} ({cabin_label(analysis.cabin)}): no affordable option right now, so there is no plan to build.")
                continue
            best = analysis.affordable[0]
            text = render_plan(analysis, best, balances=balances, generated_at=now, urgency=assess_urgency(best))
            path = save_plan(text, trip, generated_at=now, cabin=analysis.cabin, directory=plans_dir)
            print(f"Plan saved to {path}\n")
            print(text)
        return 0

    path = latest_plan(trip["id"], plans_dir, cabin)
    if path is None:
        print(
            f"No saved plan for {trip['name']}. Plans are saved when an alert is sent, or run:\n"
            f"  python -m flight_optimizer plan {trip['id']} --refresh"
        )
        return 1
    print(f"Plan file: {path}\n")
    print(path.read_text())
    return 0


def _run_trip(conn, args) -> int:
    action = getattr(args, "trip_action", None)
    if action in (None, "list"):
        print(format_trips(conn))
        return 0

    if action == "add":
        depart = parse_date(args.depart, "--depart")
        return_date = parse_date(args.return_date, "--return") if args.return_date else None
        trip_id = add_trip(
            conn, name=args.name, origin=args.origin, destination=args.destination, depart_date=depart,
            return_date=return_date, gateways=args.gateways, active=not args.inactive,
        )
        trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        gateways = db.get_gateways(conn, trip_id)
        print(f"Added trip {trip_id}: {trip['name']}, {trip['origin']} > {trip['destination']}, "
              f"{depart:%b %d %Y}" + (f" - {return_date:%b %d %Y}" if return_date else " (one way)")
              + ", priced in economy, premium economy and business.")
        print(f"Gateways: {', '.join(gateways) if gateways else 'none'}")
        if not gateways:
            print(f"No gateways: only the direct {trip['origin']} > {trip['destination']} pair is searched, "
                  "which often returns nothing. Add hubs with: python -m flight_optimizer gateways "
                  f"{trip_id} --add HUB ...")
        print(_gateway_cost(trip, gateways))
        print("Evaluate it with: python -m flight_optimizer report" if trip["active"]
              else f"Saved inactive. Start searching it with: python -m flight_optimizer trip set {trip_id} --active")
        return 0

    trip = _pick_trip(conn, args.trip_id)
    active = True if args.active else False if args.inactive else None
    changes = dict(
        name=args.name,
        depart_date=parse_date(args.depart, "--depart") if args.depart else None,
        return_date=parse_date(args.return_date, "--return") if args.return_date else None,
        one_way=args.one_way, active=active,
    )
    given = (args.name, args.depart, args.return_date, args.one_way, args.active, args.inactive)
    if not any(given):   # judged from what was typed: --inactive means "change it", though its value is False
        print("Nothing to change: give at least one of --name, --depart, --return, --one-way, --active, --inactive.")
        return 1
    db.update_trip(conn, trip["id"], **changes)
    print("Updated.\n")
    print(format_trips(conn))
    return 0


def _run_balance(conn, args) -> int:
    """No arguments: show every balance. Otherwise set the given balances, all or nothing."""
    if not args.pairs:
        print(format_balances(db.get_balances(conn), db.balance_updates(conn)))
        return 0
    if len(args.pairs) % 2:
        raise ValueError("Give balances as pairs: PROGRAM MILES, e.g. balance qantas 12000 delta 15000.")

    changes: dict[str, int] = {}
    for program_text, miles_text in zip(args.pairs[::2], args.pairs[1::2]):
        program = resolve_program(program_text)
        if program in changes:
            raise ValueError(f"{PROGRAM_NAMES[program]} was given twice.")
        changes[program] = parse_miles(miles_text)

    before = db.get_balances(conn)
    for program, miles in changes.items():  # everything was validated above, so this is all or nothing
        db.set_balance(conn, program, miles)
    for program, miles in changes.items():
        name = PROGRAM_NAMES[program]
        print(f"{name}: unchanged at {miles:,}" if before[program] == miles
              else f"{name}: {before[program]:,} -> {miles:,}")
    print("The next check will use the new balances.")
    return 0


def _gateway_cost(trip, gateways: list[str]) -> str:
    directions = 2 if trip["return_date"] else 1
    return (
        f"Up to {directions * (1 + 2 * len(gateways))} Seats.aero searches per run before caching "
        f"(the direct pair, plus up to {2 * directions} for each gateway)."
    )


def _run_gateways(conn, args) -> int:
    trip = _pick_trip(conn, args.trip_id)
    current = db.get_gateways(conn, trip["id"])
    codes = [c.strip().upper() for c in (args.set or args.add or args.remove or [])]

    if args.set:
        new = list(dict.fromkeys(codes))
    elif args.add:
        new = current + [c for c in dict.fromkeys(codes) if c not in current]
    elif args.remove:
        missing = [c for c in codes if c not in current]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} is not a gateway for {trip['name']} "
                f"(current: {', '.join(current) or 'none'}). Nothing was changed."
            )
        new = [c for c in current if c not in codes]
    else:
        new = None

    if new is not None and sorted(new) != sorted(current):
        db.set_gateways(conn, trip["id"], new)  # validates the airport codes
        current = db.get_gateways(conn, trip["id"])
        print(f"Gateways updated for {trip['name']}.")
    elif new is not None:
        print("No change: those are already the gateways.")

    print(f"{trip['name']} (trip {trip['id']}): {trip['origin']} > {trip['destination']}")
    print(f"Gateways: {', '.join(current) if current else 'none'}")
    if not current:
        print(f"No gateways: only the direct {trip['origin']} > {trip['destination']} pair is searched, "
              "which often returns nothing for smaller airports.")
    print(_gateway_cost(trip, current))
    return 0


_TIER_ARG = re.compile(r"^(\d[\d,]*)(?:-(\d[\d,]*)|(\+))?:(\d{1,3})$")


def _parse_tier_arg(text: str) -> PromoTier:
    match = _TIER_ARG.match(text.strip())
    if not match or (match.group(2) is None and match.group(3) is None):
        raise ValueError(f"Tier {text!r} should look like 5000-25000:50 or 80000+:100.")
    low = parse_miles(match.group(1))
    high = parse_miles(match.group(2)) if match.group(2) else None
    if high is not None and high < low:
        raise ValueError(f"Tier {text!r}: the top is below the bottom.")
    return PromoTier(low, high, int(match.group(4)))


def _promo_line(promo: Promo) -> str:
    sale = f"{'up to ' if promo.tiers_known or promo.source != 'manual' else ''}{promo.headline_percent}% {promo.kind}"
    if promo.tiers:
        detail = " | ".join(
            f"{t.min_points:,}{'+' if t.max_points is None else '-' + format(t.max_points, ',')}: {t.percent}%"
            for t in promo.tiers
        )
    else:
        detail = "headline only: the real bonus for a small purchase is unknown"
    ends = promo.ends.isoformat() if promo.ends else "no end date"
    return f"{PROGRAM_NAMES.get(promo.program, promo.program):<30} {sale:<20} {ends:<12} {detail}   [{promo.source}]"


def _run_promos(conn, args, clients: Clients, now: datetime) -> int:
    today = now.date()
    action = getattr(args, "promo_action", None)

    if action == "remove":
        program = resolve_program(args.program)
        removed = db.delete_manual_promos(conn, program)
        print(f"Removed {removed} hand-added sale(s) for {PROGRAM_NAMES[program]}." if removed
              else f"No hand-added sales for {PROGRAM_NAMES[program]}.")
        return 0

    if action == "add":
        program = resolve_program(args.program)
        try:
            ends = date.fromisoformat(args.ends)
        except ValueError:
            raise ValueError(f"--ends {args.ends!r} should look like 2026-09-30.") from None
        percent = args.bonus if args.bonus is not None else args.discount
        if not 1 <= percent <= 300:
            raise ValueError("The percent should be between 1 and 300.")
        kind = "bonus" if args.bonus is not None else "discount"
        tiers = tuple(sorted((_parse_tier_arg(t) for t in args.tier), key=lambda t: t.min_points))
        save_promo(conn, Promo(
            key=f"manual:{program}:{ends.isoformat()}", program=program, kind=kind, headline_percent=percent,
            tiers=tiers, ends=ends, title=f"Added by hand: {percent}% {kind}", url=args.url, source="manual",
            last_seen=today,
        ))
        print(f"Recorded a {percent}% {kind} sale for {PROGRAM_NAMES[program]} ending {ends}.")
        if not tiers:
            print("No tiers given, so it is treated as a best case and will not trigger alerts. "
                  "Add --tier options from the sale's page to price it properly.")
        if program not in RETAIL_PRICES:
            print(f"Note: {PROGRAM_NAMES[program]} has no retail price on file, so it cannot be used in cost estimates.")
        return 0

    if not args.no_fetch and clients.promos is not None:
        result = refresh_promos(conn, clients.promos, now, force=True)
        print(f"Read {result.pages_read} roundup page(s); {result.new_or_updated} sale(s) found or refreshed.")
        for problem in result.errors:
            print(f"  could not read: {problem}", file=sys.stderr)
    promos = sorted(best_promos(active_promos(conn, today)).values(), key=lambda p: p.program)
    if not promos:
        print("No live buy-miles sales on record.")
        return 0
    print(f"{'Program':<30} {'Sale':<20} {'Ends':<12} Tiers")
    for promo in promos:
        print(_promo_line(promo))
    print("\nSales are read from deal sites' prose, so confirm terms on the program's page. "
          "See what they mean for your trips with: python -m flight_optimizer report")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    env_file: str | None = ".env",
    clients_factory=default_clients,
    plans_dir: Path | None = None,
    now: datetime | None = None,
) -> int:
    args = _build_parser().parse_args(argv)

    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    conn = db.connect(settings.database_path)
    db.init_schema(conn)
    plans_dir = plans_dir or settings.database_path.resolve().parent / "plans"
    now = now or datetime.now()
    fresh = args.fresh or getattr(args, "refresh", False)  # a refreshed plan must be fresh

    # Local-only commands: no API clients needed.
    if args.command in ("balance", "gateways", "trip"):
        try:
            handler = {"balance": _run_balance, "gateways": _run_gateways, "trip": _run_trip}[args.command]
            return handler(conn, args)
        except (ValueError, LookupError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    clients = clients_factory(settings, conn, fresh)

    try:
        if args.command == "done":
            trip = _pick_trip(conn, args.trip_id)
            db.set_trip_active(conn, trip["id"], False)
            print(f"{trip['name']} marked inactive: it will no longer be searched or alerted on.")
            return 0
        if args.command == "test-notification":
            if clients.ntfy is None:
                print("error: NTFY_TOPIC is not set. Add it to .env (see .env.example).", file=sys.stderr)
                return 1
            clients.ntfy.send(
                "If you can read this, notifications from Flight Optimizer are working.",
                title="Flight Optimizer test", tags=("white_check_mark",),
            )
            print("Test notification sent. Check your phone.")
            return 0
        if args.command == "digest":
            return _run_digest(conn, clients, now, dry_run=args.dry_run)
        if args.command == "promos":
            try:
                return _run_promos(conn, args, clients, now)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        if args.command == "notify":
            return _run_notify(conn, clients, plans_dir, dry_run=args.dry_run, now=now)
        if args.command == "plan":
            return _run_plan(conn, clients, plans_dir, args, now)
        if args.command == "advise":
            return _run_advise(conn, clients, args, now)
        print(build_report(conn, clients.awards, clients.cash, top_n=args.top))
        return 0
    except (SeatsAeroError, NtfyError, AdvisorError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
