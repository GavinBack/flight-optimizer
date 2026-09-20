"""Client for the Seats.aero Partner API (cached award availability).

Pro accounts get 1,000 calls per day for personal use. Every response carries
an `x-ratelimit-remaining` header, exposed here as `SeatsAeroClient.calls_remaining`.
Results are cached snapshots, not live: check `AwardResult.updated_at`.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

import requests

BASE_URL = "https://seats.aero/partnerapi"


class Cabin(str, Enum):
    """Value is the field prefix Seats.aero uses (e.g. `JMileageCost`)."""

    ECONOMY = "Y"
    PREMIUM_ECONOMY = "W"
    BUSINESS = "J"
    FIRST = "F"


class SeatsAeroError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SeatsAeroAuthError(SeatsAeroError):
    """Bad or missing key, or the account has no API access."""


class SeatsAeroRateLimitError(SeatsAeroError):
    """Daily call quota used up (resets at midnight UTC)."""


@dataclass(frozen=True)
class AwardResult:
    """One cabin's availability on one route, date, and program."""

    source: str  # Seats.aero program id, e.g. "united", "british"
    origin: str
    destination: str
    date: date
    cabin: Cabin
    miles: int
    total_taxes: int  # as returned by the API; units not verified
    currency: str
    remaining_seats: int
    airlines: str
    direct: bool
    updated_at: str  # when Seats.aero last refreshed this row
    # Filled in only after a trips lookup (see FlightOption / SeatsAeroClient.trips).
    availability_id: str = ""  # Seats.aero row id, needed for that lookup
    departs_at: datetime | None = None  # local time at the origin airport
    arrives_at: datetime | None = None  # local time at the destination airport
    flight_numbers: str = ""
    booking_url: str = ""  # pre-filled search on the program's own site


@dataclass(frozen=True)
class SearchPage:
    results: list[AwardResult]
    row_count: int  # raw rows on this page; `skip` counts rows, not results
    has_more: bool
    cursor: int | None


def parse_availability(row: dict) -> list[AwardResult]:
    """Flatten one API row into one AwardResult per available cabin."""
    route = row["Route"]
    results = []
    for cabin in Cabin:
        prefix = cabin.value
        if not row.get(f"{prefix}Available"):
            continue
        results.append(
            AwardResult(
                source=row["Source"],
                origin=route["OriginAirport"],
                destination=route["DestinationAirport"],
                date=date.fromisoformat(row["Date"]),
                cabin=cabin,
                miles=int(row[f"{prefix}MileageCost"]),
                total_taxes=int(row.get(f"{prefix}TotalTaxes") or 0),
                currency=row.get("TaxesCurrency") or "",
                remaining_seats=int(row.get(f"{prefix}RemainingSeats") or 0),
                airlines=row.get(f"{prefix}Airlines") or "",
                direct=bool(row.get(f"{prefix}Direct")),
                updated_at=row.get("UpdatedAt") or "",
                availability_id=row.get("ID") or "",
            )
        )
    return results


_CABIN_BY_NAME = {
    "economy": Cabin.ECONOMY,
    "premium_economy": Cabin.PREMIUM_ECONOMY,
    "premium": Cabin.PREMIUM_ECONOMY,
    "business": Cabin.BUSINESS,
    "first": Cabin.FIRST,
}


@dataclass(frozen=True)
class FlightOption:
    """One bookable flight (or connecting set of flights) behind a search result."""

    cabin: Cabin
    miles: int
    seats: int
    departs_at: datetime  # local time at the origin
    arrives_at: datetime  # local time at the destination
    flight_numbers: str
    stops: int
    total_duration: int  # minutes
    total_taxes: int
    currency: str
    updated_at: str


@dataclass(frozen=True)
class TripDetails:
    options: tuple[FlightOption, ...]
    booking_url: str  # the primary (program's own) pre-filled link, or ""
    booking_links: tuple[tuple[str, str], ...]  # (label, url) for every program that can book it
    from_cache: bool = False  # True when reused from the local cache (no API call)


def _local_time(value: str) -> datetime:
    """Seats.aero tags times 'Z' but they are local to each airport (LAX 22:55 -> YYZ 06:41
    on a 4h46m flight). Fine for a connection at one airport, wrong for comparing across zones."""
    return datetime.fromisoformat(value.removesuffix("Z"))


def parse_trips(body: dict) -> TripDetails:
    options = []
    for entry in body.get("data", []):
        cabin = _CABIN_BY_NAME.get(str(entry.get("Cabin", "")).lower())
        if cabin is None or not entry.get("DepartsAt") or not entry.get("ArrivesAt"):
            continue
        miles = int(entry.get("MileageCost") or 0)
        if miles <= 0:
            continue
        options.append(
            FlightOption(
                cabin=cabin,
                miles=miles,
                seats=int(entry.get("RemainingSeats") or 0),
                departs_at=_local_time(entry["DepartsAt"]),
                arrives_at=_local_time(entry["ArrivesAt"]),
                flight_numbers=entry.get("FlightNumbers") or "",
                stops=int(entry.get("Stops") or 0),
                total_duration=int(entry.get("TotalDuration") or 0),
                total_taxes=int(entry.get("TotalTaxes") or 0),
                currency=entry.get("TaxesCurrency") or "",
                updated_at=entry.get("UpdatedAt") or "",
            )
        )
    links = [(l.get("label", ""), l["link"]) for l in body.get("booking_links") or [] if l.get("link")]
    primary = next((l["link"] for l in body.get("booking_links") or [] if l.get("primary") and l.get("link")), "")
    return TripDetails(options=tuple(options), booking_url=primary or (links[0][1] if links else ""), booking_links=tuple(links))


class SeatsAeroClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Updated after every response; None until the first call.
        self.calls_remaining: int | None = None
        self.calls_made = 0  # API calls made by this client object

    def _get(self, path: str, params: dict) -> dict:
        self.calls_made += 1
        response = self._session.get(
            f"{self._base_url}/{path}",
            params=params,
            headers={"Partner-Authorization": self._api_key},
            timeout=self._timeout,
        )

        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            self.calls_remaining = int(remaining)

        status = response.status_code
        if status in (401, 403):
            raise SeatsAeroAuthError(
                "Seats.aero rejected the API key (or the account lacks API access)",
                status,
            )
        if status == 429:
            raise SeatsAeroRateLimitError("Daily Seats.aero call quota exhausted", status)
        if status >= 400:
            raise SeatsAeroError(f"Seats.aero returned HTTP {status}: {response.text[:200]}", status)
        return response.json()

    def trips(self, availability_id: str, version: str | None = None) -> TripDetails:
        """Flight-level details (times, cost, seats, booking links) behind one search
        result. Costs one API call. `version` (the row's UpdatedAt) is ignored here; it
        lets a caching wrapper share this signature."""
        return parse_trips(self._get(f"trips/{availability_id}", {}))

    def search_page(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        *,
        sources: list[str] | None = None,
        take: int = 100,
        skip: int = 0,
        cursor: int | None = None,
    ) -> SearchPage:
        params: dict = {
            "origin_airport": origin,
            "destination_airport": destination,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "take": take,
            "skip": skip,
        }
        if sources:
            params["sources"] = ",".join(sources)
        if cursor is not None:
            params["cursor"] = cursor

        body = self._get("search", params)
        rows = body.get("data", [])
        return SearchPage(
            results=[r for row in rows for r in parse_availability(row)],
            row_count=len(rows),
            has_more=bool(body.get("hasMore")),
            cursor=body.get("cursor"),
        )

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
        """All available results for a route and date range, across pages.

        Each page costs one API call. Raises rather than silently truncating
        if `max_pages` isn't enough.
        """
        results: list[AwardResult] = []
        skip = 0
        cursor: int | None = None

        for _ in range(max_pages):
            page = self.search_page(
                origin,
                destination,
                start_date,
                end_date,
                sources=sources,
                take=take,
                skip=skip,
                cursor=cursor,
            )
            results.extend(page.results)
            if not page.has_more or page.row_count == 0:
                return results
            skip += page.row_count
            # Reuse the first page's cursor so every page reads the same snapshot.
            cursor = cursor if cursor is not None else page.cursor

        raise SeatsAeroError(
            f"Still more results after {max_pages} pages; raise max_pages or narrow the search"
        )
