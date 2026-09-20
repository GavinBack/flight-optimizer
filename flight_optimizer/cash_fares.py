"""Cash fares from SerpApi's Google Flights API, and the baseline used to value awards.

The free plan allows 250 searches a month, so every search is cached in SQLite
(`get_fares`). Errors are re-raised without the underlying exception text,
because `requests` puts the full URL, including the api_key, in its messages.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone

import requests

from flight_optimizer import db
from flight_optimizer.seats_aero import Cabin

SEARCH_URL = "https://serpapi.com/search.json"

TRAVEL_CLASS = {
    Cabin.ECONOMY: 1,
    Cabin.PREMIUM_ECONOMY: 2,
    Cabin.BUSINESS: 3,
    Cabin.FIRST: 4,
}

# Baseline rule: the cheapest fare with a reasonable trip length. Without a limit the
# cheapest business fare on RNO>SJO was $618 with an 11-hour layover; with these
# limits it's $1,505. Changing them changes every savings figure.
DEFAULT_MAX_LAYOVER_MINUTES = 240
DEFAULT_MAX_TOTAL_MINUTES = 900

# Cash prices move slowly compared with award space, and searches are scarce.
DEFAULT_MAX_CACHE_AGE = timedelta(days=7)

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SerpApiError(Exception):
    pass


class SerpApiAuthError(SerpApiError):
    """Bad or missing key."""


class SerpApiQuotaError(SerpApiError):
    """Monthly search quota used up (or too many requests per hour)."""


@dataclass(frozen=True)
class CashFare:
    price: int  # whole USD
    total_duration: int  # minutes
    layovers: tuple[int, ...]  # minutes, one per connection
    airlines: tuple[str, ...]
    flight_numbers: tuple[str, ...]
    cabins: tuple[str, ...]  # per-segment label, e.g. "First Class" on domestic legs

    @property
    def stops(self) -> int:
        return len(self.layovers)


@dataclass(frozen=True)
class CashSearch:
    fares: tuple[CashFare, ...]
    lowest_price: int | None  # Google's own figure, before any of our limits
    typical_range: tuple[int, int] | None
    fetched_at: str
    from_cache: bool = False


def parse_fare(item: dict) -> CashFare | None:
    """One `best_flights` / `other_flights` entry. None if Google gave no price."""
    if item.get("price") is None:
        return None
    segments = item.get("flights", [])
    return CashFare(
        price=int(item["price"]),
        total_duration=int(
            item.get("total_duration") or sum(s.get("duration", 0) for s in segments)
        ),
        layovers=tuple(int(layover["duration"]) for layover in item.get("layovers", [])),
        airlines=tuple(s.get("airline", "") for s in segments),
        flight_numbers=tuple(s.get("flight_number", "") for s in segments),
        cabins=tuple(s.get("travel_class", "") for s in segments),
    )


class SerpApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        url: str = SEARCH_URL,
        timeout: float = 90,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._url = url
        self._timeout = timeout
        self.searches_made = 0  # this run; the monthly total lives on SerpApi's dashboard

    def search(
        self, origin: str, destination: str, on_date: date, cabin: Cabin, *, currency: str = "USD"
    ) -> CashSearch:
        """One one-way search. Costs one of the monthly searches."""
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": on_date.isoformat(),
            "type": 2,  # one way
            "travel_class": TRAVEL_CLASS[cabin],
            "currency": currency,
            "hl": "en",
            "api_key": self._api_key,
        }
        try:
            response = self._session.get(self._url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise SerpApiError(f"SerpApi request failed ({type(exc).__name__})") from None
        self.searches_made += 1

        if response.status_code in (401, 403):
            raise SerpApiAuthError("SerpApi rejected the API key")
        if response.status_code == 429:
            raise SerpApiQuotaError("SerpApi search quota exhausted")
        try:
            body = response.json()
        except ValueError:
            raise SerpApiError(f"SerpApi returned HTTP {response.status_code} with no JSON") from None

        error = body.get("error")
        if error:
            if "hasn't returned any results" in error.lower():
                return CashSearch(
                    fares=(), lowest_price=None, typical_range=None, fetched_at=_now_string()
                )
            raise SerpApiError(f"SerpApi error: {error}")
        if response.status_code >= 400:
            raise SerpApiError(f"SerpApi returned HTTP {response.status_code}")

        fares = tuple(
            fare
            for item in body.get("best_flights", []) + body.get("other_flights", [])
            if (fare := parse_fare(item)) is not None
        )
        insights = body.get("price_insights") or {}
        typical = insights.get("typical_price_range")
        return CashSearch(
            fares=fares,
            lowest_price=insights.get("lowest_price"),
            typical_range=(typical[0], typical[1]) if typical and len(typical) == 2 else None,
            fetched_at=_now_string(),
        )


def _now_string(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(_TIME_FORMAT)


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def _fare_from_dict(data: dict) -> CashFare:
    return CashFare(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


def get_fares(
    conn: sqlite3.Connection,
    client: SerpApiClient,
    origin: str,
    destination: str,
    on_date: date,
    cabin: Cabin,
    *,
    max_age: timedelta = DEFAULT_MAX_CACHE_AGE,
    now: datetime | None = None,
) -> CashSearch:
    """Cached fares if fresh enough, otherwise search and cache."""
    now = now or datetime.now(timezone.utc)
    cabin_key = cabin.name.lower()

    cached = db.get_cash_fares(conn, origin, destination, on_date, cabin_key)
    if cached is not None and now - _parse_time(cached["fetched_at"]) <= max_age:
        low, high = cached["typical_low"], cached["typical_high"]
        return CashSearch(
            fares=tuple(_fare_from_dict(f) for f in json.loads(cached["fares_json"])),
            lowest_price=cached["lowest_price"],
            typical_range=(low, high) if low is not None and high is not None else None,
            fetched_at=cached["fetched_at"],
            from_cache=True,
        )

    search = replace(
        client.search(origin, destination, on_date, cabin), fetched_at=_now_string(now)
    )
    db.save_cash_fares(
        conn,
        origin=origin,
        destination=destination,
        travel_date=on_date,
        cabin=cabin_key,
        fares_json=json.dumps([asdict(f) for f in search.fares]),
        lowest_price=search.lowest_price,
        typical_low=search.typical_range[0] if search.typical_range else None,
        typical_high=search.typical_range[1] if search.typical_range else None,
        fetched_at=search.fetched_at,
    )
    return search


def cheapest_reasonable(
    fares: tuple[CashFare, ...] | list[CashFare],
    *,
    max_layover_minutes: int = DEFAULT_MAX_LAYOVER_MINUTES,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
) -> CashFare | None:
    """The cash baseline: cheapest fare with no long layover and a sane total time."""
    reasonable = [
        f
        for f in fares
        if f.total_duration <= max_total_minutes
        and all(layover <= max_layover_minutes for layover in f.layovers)
    ]
    return min(reasonable, key=lambda f: (f.price, f.total_duration), default=None)
