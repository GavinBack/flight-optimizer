"""Command line.

    python -m flight_optimizer [report] [--top N]   report on every active trip
    python -m flight_optimizer notify [--dry-run]   alert (via ntfy) on anything new
    python -m flight_optimizer plan [TRIP] [--refresh]   show the booking plan
    python -m flight_optimizer done TRIP            mark a trip booked (stops alerts)
    python -m flight_optimizer test-notification    check that ntfy reaches your phone
    python -m flight_optimizer balance [PROGRAM MILES ...]   show balances, or set them
    python -m flight_optimizer gateways [TRIP] [--set|--add|--remove AIRPORT ...]   show or change hubs

Add --fresh to any command to skip the 1-hour search cache.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flight_optimizer import db
from flight_optimizer.alerts import assess_urgency, decide_alert, record_sent
from flight_optimizer.analysis import analyze_trip
from flight_optimizer.cash_fares import SerpApiClient
from flight_optimizer.config import ConfigError, Settings, load_settings
from flight_optimizer.notify import NtfyClient, NtfyError
from flight_optimizer.plans import latest_plan, render_plan, save_plan
from flight_optimizer.programs import PROGRAM_NAMES, format_balances, parse_miles, resolve_program
from flight_optimizer.report import build_report
from flight_optimizer.seats_aero import SeatsAeroClient, SeatsAeroError
from flight_optimizer.seats_cache import CachedSeatsAero


@dataclass
class Clients:
    awards: SeatsAeroClient
    cash: SerpApiClient | None
    ntfy: NtfyClient | None


def default_clients(settings: Settings, conn, fresh: bool = False) -> Clients:
    """Real clients. Seats.aero calls go through a local cache; `fresh` skips the
    short-lived search cache (flight details Seats.aero hasn't changed are still reused)."""
    return Clients(
        awards=CachedSeatsAero(
            SeatsAeroClient(settings.seats_aero_api_key), conn, use_search_cache=not fresh
        ),
        cash=SerpApiClient(settings.serpapi_api_key) if settings.serpapi_api_key else None,
        ntfy=NtfyClient(settings.ntfy_topic, server=settings.ntfy_server) if settings.ntfy_topic else None,
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

    done = commands.add_parser("done", help="mark a trip booked, so it stops being searched and alerted on")
    done.add_argument("trip_id", type=int)

    commands.add_parser("test-notification", help="send a test message to your ntfy topic")

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


def _run_notify(conn, clients: Clients, plans_dir: Path, *, dry_run: bool, now: datetime) -> int:
    trips = db.active_trips(conn)
    if not trips:
        print("No active trips. Mark a trip active to be alerted about it.")
        return 0
    if not dry_run and clients.ntfy is None:
        print("error: NTFY_TOPIC is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1

    balances = db.get_balances(conn)
    failed = False
    for trip in trips:
        analysis = analyze_trip(conn, clients.awards, clients.cash, trip, balances)
        alert = decide_alert(conn, analysis, now=now)
        if alert is None:
            print(f"{trip['name']}: nothing new to alert on.")
            continue
        if dry_run:
            print(f"--- would send for {trip['name']} (priority {alert.priority}) ---")
            print(alert.title)
            print(alert.message)
            continue

        # Save the plan first, so the alert's "full steps" pointer is always valid.
        plan_text = render_plan(analysis, alert.option, balances=balances, generated_at=now, urgency=alert.urgency)
        path = save_plan(plan_text, trip, generated_at=now, directory=plans_dir)
        try:
            clients.ntfy.send(
                alert.message, title=alert.title, priority=alert.priority, tags=alert.tags, click=alert.click
            )
        except NtfyError as exc:
            print(f"{trip['name']}: could not send the alert: {exc}", file=sys.stderr)
            failed = True  # not recorded, so the next run tries again
            continue
        record_sent(conn, alert, str(path))
        print(f"{trip['name']}: alert sent ({alert.urgency.label}). Plan saved to {path}")
    return 1 if failed else 0


def _run_plan(conn, clients: Clients, plans_dir: Path, args, now: datetime) -> int:
    trip = _pick_trip(conn, args.trip_id)
    if args.refresh:
        balances = db.get_balances(conn)
        analysis = analyze_trip(conn, clients.awards, clients.cash, trip, balances)
        if not analysis.affordable:
            print(f"{trip['name']}: no affordable option right now, so there is no plan to build.")
            return 0
        best = analysis.affordable[0]
        text = render_plan(analysis, best, balances=balances, generated_at=now, urgency=assess_urgency(best))
        path = save_plan(text, trip, generated_at=now, directory=plans_dir)
        print(f"Plan saved to {path}\n")
        print(text)
        return 0

    path = latest_plan(trip["id"], plans_dir)
    if path is None:
        print(
            f"No saved plan for {trip['name']}. Plans are saved when an alert is sent, or run:\n"
            f"  python -m flight_optimizer plan {trip['id']} --refresh"
        )
        return 1
    print(f"Plan file: {path}\n")
    print(path.read_text())
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
    if args.command in ("balance", "gateways"):
        try:
            return _run_balance(conn, args) if args.command == "balance" else _run_gateways(conn, args)
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
        if args.command == "notify":
            return _run_notify(conn, clients, plans_dir, dry_run=args.dry_run, now=now)
        if args.command == "plan":
            return _run_plan(conn, clients, plans_dir, args, now)
        print(build_report(conn, clients.awards, clients.cash, top_n=args.top))
        return 0
    except (SeatsAeroError, NtfyError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
