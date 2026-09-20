"""Client for the Seats.aero Partner API (cached award availability).

Pro accounts get 1,000 calls per day for personal use. Every response carries
an `x-ratelimit-remaining` header, exposed here as `SeatsAeroClient.calls_remaining`.
Results are cached snapshots, not live: check `AwardResult.updated_at`.
"""

from dataclasses import dataclass
from datetime import date
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
            )
        )
    return results


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

    def _get(self, path: str, params: dict) -> dict:
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
