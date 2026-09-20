import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from flight_optimizer import db
from flight_optimizer.cash_fares import (
    CashFare,
    SerpApiAuthError,
    SerpApiClient,
    SerpApiError,
    SerpApiQuotaError,
    cheapest_reasonable,
    get_fares,
    parse_fare,
)
from flight_optimizer.seats_aero import Cabin

KEY = "sekrit-key-123"
JAN9 = date(2027, 1, 9)


def raw_fare(price=618, layovers=(661,), total=1215, cabin="Business Class"):
    """Shaped like a real `best_flights` entry."""
    segments = [
        {"airline": "United", "flight_number": f"UA {700 + i}", "travel_class": cabin, "duration": 200}
        for i in range(len(layovers) + 1)
    ]
    item = {
        "flights": segments,
        "layovers": [{"id": "ORD", "duration": m} for m in layovers],
        "total_duration": total,
    }
    if price is not None:
        item["price"] = price
    return item


def fare(price, layovers=(), total=600):
    return CashFare(price=price, total_duration=total, layovers=tuple(layovers),
                    airlines=("United",), flight_numbers=("UA 1",), cabins=("Economy",))


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params})
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(*responses):
    session = FakeSession(*responses)
    return SerpApiClient(KEY, session=session), session


def ok_body(best=(), other=(), lowest=618, typical=(540, 650)):
    return {
        "best_flights": list(best),
        "other_flights": list(other),
        "price_insights": {"lowest_price": lowest, "typical_price_range": list(typical)},
    }


def search(client, cabin=Cabin.BUSINESS):
    return client.search("RNO", "SJO", JAN9, cabin)


# --- parsing -----------------------------------------------------------------

def test_parse_fare_reads_price_layovers_and_segments():
    parsed = parse_fare(raw_fare(price=769, layovers=(646, 161), total=1376))

    assert parsed.price == 769
    assert parsed.layovers == (646, 161)
    assert parsed.stops == 2
    assert parsed.total_duration == 1376
    assert parsed.airlines == ("United",) * 3
    assert parsed.cabins == ("Business Class",) * 3


def test_parse_fare_without_a_price_is_skipped():
    assert parse_fare(raw_fare(price=None)) is None


# --- baseline rule -----------------------------------------------------------

def test_baseline_skips_long_layovers_and_long_trips():
    fares = [
        fare(618, layovers=(661,), total=700),   # 11-hour layover
        fare(700, layovers=(60,), total=1000),   # too long overall
        fare(1505, layovers=(238, 85), total=795),
        fare(1600, layovers=(30,), total=500),
    ]

    assert cheapest_reasonable(fares).price == 1505


def test_baseline_limits_are_inclusive_and_configurable():
    fares = [fare(500, layovers=(240,), total=900)]

    assert cheapest_reasonable(fares).price == 500
    assert cheapest_reasonable(fares, max_layover_minutes=239) is None
    assert cheapest_reasonable(fares, max_total_minutes=899) is None


def test_baseline_never_uses_a_layover_of_an_hour_or_less():
    fares = [
        fare(300, layovers=(60,)),        # exactly 1 hour
        fare(310, layovers=(45, 200)),    # one short connection is enough to exclude it
        fare(320, layovers=(61,)),        # just over 1 hour is allowed (the report warns)
        fare(900, layovers=(180,)),
    ]

    assert cheapest_reasonable(fares).price == 320
    assert cheapest_reasonable([fares[0], fares[1]]) is None


def test_baseline_prefers_shorter_trip_on_equal_price_and_handles_none():
    fares = [fare(323, total=739), fare(323, total=712)]

    assert cheapest_reasonable(fares).total_duration == 712
    assert cheapest_reasonable([]) is None


# --- client ------------------------------------------------------------------

def test_search_sends_expected_params_and_parses_results():
    client, session = make_client(FakeResponse(ok_body(best=[raw_fare(618)], other=[raw_fare(1505, (238,), 795)])))

    result = search(client)

    params = session.requests[0]["params"]
    assert params["engine"] == "google_flights"
    assert (params["departure_id"], params["arrival_id"]) == ("RNO", "SJO")
    assert params["outbound_date"] == "2027-01-09"
    assert params["type"] == 2 and params["travel_class"] == 3
    assert [f.price for f in result.fares] == [618, 1505]
    assert result.lowest_price == 618
    assert result.typical_range == (540, 650)
    assert client.searches_made == 1


def test_economy_uses_travel_class_1():
    client, session = make_client(FakeResponse(ok_body()))

    search(client, Cabin.ECONOMY)

    assert session.requests[0]["params"]["travel_class"] == 1


def test_client_requires_a_key():
    with pytest.raises(ValueError):
        SerpApiClient("")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures(status):
    client, _ = make_client(FakeResponse({"error": "Invalid API key"}, status_code=status))

    with pytest.raises(SerpApiAuthError):
        search(client)


def test_quota_exhausted():
    client, _ = make_client(FakeResponse({"error": "Your searches are exhausted"}, status_code=429))

    with pytest.raises(SerpApiQuotaError):
        search(client)


def test_no_results_is_an_empty_search_not_an_error():
    client, _ = make_client(FakeResponse({"error": "Google Flights hasn't returned any results for this query."}))

    result = search(client)

    assert result.fares == () and result.lowest_price is None


def test_other_errors_raise():
    client, _ = make_client(FakeResponse({"error": "Something broke"}))

    with pytest.raises(SerpApiError, match="Something broke"):
        search(client)


def test_network_errors_never_leak_the_api_key():
    leaky = requests.ConnectionError(f"Max retries exceeded with url: /search.json?api_key={KEY}")
    client, _ = make_client(leaky)

    with pytest.raises(SerpApiError) as excinfo:
        search(client)

    assert KEY not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


def test_non_json_response_raises():
    client, _ = make_client(FakeResponse(None, status_code=502))

    with pytest.raises(SerpApiError, match="502"):
        search(client)


# --- cache -------------------------------------------------------------------

@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def test_second_lookup_is_served_from_cache_without_a_search(conn):
    client, session = make_client(FakeResponse(ok_body(best=[raw_fare(618)])))

    first = get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW)
    second = get_fares(conn, client, "rno", "sjo", JAN9, Cabin.BUSINESS, now=NOW + timedelta(days=1))

    assert not first.from_cache and second.from_cache
    assert len(session.requests) == 1
    assert second.fares == first.fares
    assert second.typical_range == (540, 650) and second.lowest_price == 618


def test_stale_cache_triggers_a_new_search_and_replaces_the_row(conn):
    client, session = make_client(
        FakeResponse(ok_body(best=[raw_fare(618)])), FakeResponse(ok_body(best=[raw_fare(700)]))
    )
    get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW)

    later = get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW + timedelta(days=8))

    assert not later.from_cache and later.fares[0].price == 700
    assert len(session.requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM cash_fare_cache").fetchone()[0] == 1


def test_cache_is_keyed_by_cabin_and_date(conn):
    client, session = make_client(
        FakeResponse(ok_body(best=[raw_fare(618)])), FakeResponse(ok_body(best=[raw_fare(313)])),
        FakeResponse(ok_body(best=[raw_fare(650)])),
    )

    get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW)
    get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.ECONOMY, now=NOW)
    get_fares(conn, client, "RNO", "SJO", date(2027, 1, 10), Cabin.BUSINESS, now=NOW)

    assert len(session.requests) == 3


def test_cache_stores_every_fare_so_limits_can_change_without_searching(conn):
    client, session = make_client(
        FakeResponse(ok_body(best=[raw_fare(618, (661,), 1215)], other=[raw_fare(1505, (238,), 795)]))
    )
    get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW)

    cached = get_fares(conn, client, "RNO", "SJO", JAN9, Cabin.BUSINESS, now=NOW)

    assert cheapest_reasonable(cached.fares).price == 1505
    assert cheapest_reasonable(cached.fares, max_layover_minutes=1000, max_total_minutes=2000).price == 618
    assert len(session.requests) == 1
    assert json.loads(conn.execute("SELECT fares_json FROM cash_fare_cache").fetchone()[0])
