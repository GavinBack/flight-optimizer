"""Run the report: `python -m flight_optimizer [--top N]`."""

import argparse
import sys

from flight_optimizer import db
from flight_optimizer.cash_fares import SerpApiClient
from flight_optimizer.config import ConfigError, load_settings
from flight_optimizer.report import build_report
from flight_optimizer.seats_aero import SeatsAeroClient, SeatsAeroError


def main(argv: list[str] | None = None, *, env_file: str | None = ".env") -> int:
    parser = argparse.ArgumentParser(
        prog="python -m flight_optimizer",
        description="Report the best affordable award options for each active trip.",
    )
    parser.add_argument("--top", type=int, default=5, help="award options to show per trip (default 5)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    conn = db.connect(settings.database_path)
    db.init_schema(conn)
    awards = SeatsAeroClient(settings.seats_aero_api_key)
    cash = SerpApiClient(settings.serpapi_api_key) if settings.serpapi_api_key else None

    try:
        print(build_report(conn, awards, cash, top_n=args.top))
    except SeatsAeroError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
