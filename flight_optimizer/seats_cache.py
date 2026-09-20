"""A caching layer over `SeatsAeroClient`, with the same `search` / `trips` interface.

- `trips` (the expensive part: up to ~40 calls a run) is cached with no time limit,
  keyed by the search row's own `UpdatedAt` stamp. Seats.aero stamps that value onto
  every flight entry, so an unchanged stamp means unchanged flights. A row that has
  been refreshed has a new stamp and is fetched again.
- `search` has no such stamp, so results expire after `search_ttl` (default 1 hour).
  That saves repeated runs in one sitting and never serves yesterday's search to the
  daily job. `use_search_cache=False` (the CLI's `--fresh`) always searches, then
  stores the result.

Failed calls are never cached.
"""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from flight_optimizer import db
from flight_optimizer.seats_aero import (
    AwardResult,
    Cabin,
    FlightOption,
    SeatsAeroClient,
    TripDetails,
)

DEFAULT_SEARCH_TTL = timedelta(hours=1)
TRIPS_KEEP = timedelta(days=30)  # unchanged rows are reused; this only bounds table size
SEARCH_KEEP = timedelta(days=2)

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def _parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def award_to_dict(result: AwardResult) -> dict:
    return {
        "source": result.source, "origin": result.origin, "destination": result.destination,
        "date": result.date.isoformat(), "cabin": result.cabin.name, "miles": result.miles,
        "total_taxes": result.total_taxes, "currency": result.currency,
        "remaining_seats": result.remaining_seats, "airlines": result.airlines,
        "direct": result.direct, "updated_at": result.updated_at,
        "availability_id": result.availability_id, "departs_at": _iso(result.departs_at),
        "arrives_at": _iso(result.arrives_at), "flight_numbers": result.flight_numbers,
        "booking_url": result.booking_url,
    }


def award_from_dict(data: dict) -> AwardResult:
    return AwardResult(
        **{**data, "date": date.fromisoformat(data["date"]), "cabin": Cabin[data["cabin"]],
           "departs_at": _moment(data.get("departs_at")), "arrives_at": _moment(data.get("arrives_at"))}
    )


def details_to_dict(details: TripDetails) -> dict:
    return {
        "options": [
            {**{k: getattr(o, k) for k in (
                "miles", "seats", "flight_numbers", "stops", "total_duration",
                "total_taxes", "currency", "updated_at")},
             "cabin": o.cabin.name, "departs_at": o.departs_at.isoformat(),
             "arrives_at": o.arrives_at.isoformat()}
            for o in details.options
        ],
        "booking_url": details.booking_url,
        "booking_links": [list(pair) for pair in details.booking_links],
    }


def details_from_dict(data: dict) -> TripDetails:
    return TripDetails(
        options=tuple(
            FlightOption(
                **{**o, "cabin": Cabin[o["cabin"]], "departs_at": datetime.fromisoformat(o["departs_at"]),
                   "arrives_at": datetime.fromisoformat(o["arrives_at"])}
            )
            for o in data["options"]
        ),
        booking_url=data["booking_url"],
        booking_links=tuple((label, url) for label, url in data["booking_links"]),
    )


class CachedSeatsAero:
    def __init__(
        self,
        client: SeatsAeroClient,
        conn,
        *,
        search_ttl: timedelta = DEFAULT_SEARCH_TTL,
        use_search_cache: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._client = client
        self._conn = conn
        self._search_ttl = search_ttl
        self._use_search_cache = use_search_cache
        self._clock = clock
        self.search_cache_hits = 0
        self.trips_cache_hits = 0
        now = clock()
        db.prune_seats_cache(
            conn, trips_before=_stamp(now - TRIPS_KEEP), search_before=_stamp(now - SEARCH_KEEP)
        )

    @property
    def calls_remaining(self) -> int | None:
        return self._client.calls_remaining

    @property
    def api_calls(self) -> int:
        """Real Seats.aero API calls made through this object."""
        return self._client.calls_made

    @property
    def cache_hits(self) -> int:
        return self.search_cache_hits + self.trips_cache_hits

    def search(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        *,
        sources: list[str] | None = None,
        take: int = 100,
        max_pages: int = 10,
    ) -> list[AwardResult]:
        key = "|".join(
            [origin, destination, start_date.isoformat(), end_date.isoformat(),
             ",".join(sorted(sources or [])), str(take)]
        )
        now = self._clock()
        if self._use_search_cache:
            row = db.get_search_cache(self._conn, key)
            if row is not None and now - _parse_stamp(row["fetched_at"]) <= self._search_ttl:
                self.search_cache_hits += 1
                return [award_from_dict(d) for d in json.loads(row["results_json"])]

        results = self._client.search(
            origin, destination, start_date, end_date, sources=sources, take=take, max_pages=max_pages
        )
        db.save_search_cache(
            self._conn, key, json.dumps([award_to_dict(r) for r in results]), _stamp(now)
        )
        return results

    def trips(self, availability_id: str, version: str | None = None) -> TripDetails:
        """Cached by (row id, version). No version means no caching: without Seats.aero's
        stamp we can't tell whether the flights changed."""
        if version:
            row = db.get_trips_cache(self._conn, availability_id, version)
            if row is not None:
                self.trips_cache_hits += 1
                return replace(details_from_dict(json.loads(row["details_json"])), from_cache=True)

        details = self._client.trips(availability_id)
        if version:
            db.save_trips_cache(
                self._conn, availability_id, version, json.dumps(details_to_dict(details)),
                _stamp(self._clock()),
            )
        return details
