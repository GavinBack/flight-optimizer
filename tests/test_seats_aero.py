from datetime import date

import pytest

from flight_optimizer.seats_aero import (
    Cabin,
    SeatsAeroAuthError,
    SeatsAeroClient,
    SeatsAeroError,
    SeatsAeroRateLimitError,
    parse_availability,
)


def make_row(row_id="r1", *, j_available=True, f_available=False):
    """Trimmed copy of a real /search row (JFK-LHR on british)."""
    return {
        "ID": row_id,
        "Route": {"OriginAirport": "JFK", "DestinationAirport": "LHR", "Source": "british"},
        "Date": "2026-11-01",
        "YAvailable": True,
        "WAvailable": True,
        "JAvailable": j_available,
        "FAvailable": f_available,
        "YMileageCost": "33000",
        "WMileageCost": "66000",
        "JMileageCost": "99000",
        "FMileageCost": "0",
        "TaxesCurrency": "USD",
        "YTotalTaxes": 0,
        "JTotalTaxes": 0,
        "YRemainingSeats": 1,
        "WRemainingSeats": 2,
        "JRemainingSeats": 1,
        "YAirlines": "BA",
        "WAirlines": "BA",
        "JAirlines": "BA",
        "YDirect": True,
        "WDirect": True,
        "JDirect": True,
        "Source": "british",
        "UpdatedAt": "2026-05-12T10:18:45Z",
    }


class FakeResponse:
    def __init__(self, body=None, status_code=200, remaining="999"):
        self._body = body if body is not None else {}
        self.status_code = status_code
        self.headers = {"x-ratelimit-remaining": remaining} if remaining else {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Returns queued responses in order and records every request."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)


def make_client(*responses):
    session = FakeSession(*responses)
    return SeatsAeroClient("test-key", session=session), session


def search(client, **kwargs):
    return client.search("JFK", "LHR", date(2026, 11, 1), date(2026, 11, 10), **kwargs)


def test_parse_returns_one_result_per_available_cabin():
    results = parse_availability(make_row())

    assert [r.cabin for r in results] == [Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS]
    business = results[-1]
    assert business.miles == 99000
    assert business.source == "british"
    assert (business.origin, business.destination) == ("JFK", "LHR")
    assert business.date == date(2026, 11, 1)
    assert business.remaining_seats == 1
    assert business.airlines == "BA"
    assert business.direct is True
    assert business.updated_at == "2026-05-12T10:18:45Z"


def test_parse_skips_unavailable_cabins():
    results = parse_availability(make_row(j_available=False))

    assert Cabin.BUSINESS not in [r.cabin for r in results]
    assert Cabin.FIRST not in [r.cabin for r in results]


def test_client_requires_api_key():
    with pytest.raises(ValueError):
        SeatsAeroClient("")


def test_search_sends_auth_header_and_params():
    client, session = make_client(FakeResponse({"data": [], "hasMore": False}))

    search(client, sources=["united", "lifemiles"])

    request = session.requests[0]
    assert request["url"] == "https://seats.aero/partnerapi/search"
    assert request["headers"] == {"Partner-Authorization": "test-key"}
    assert request["params"]["origin_airport"] == "JFK"
    assert request["params"]["destination_airport"] == "LHR"
    assert request["params"]["start_date"] == "2026-11-01"
    assert request["params"]["end_date"] == "2026-11-10"
    assert request["params"]["sources"] == "united,lifemiles"


def test_search_paginates_with_skip_and_first_cursor():
    client, session = make_client(
        FakeResponse({"data": [make_row("a"), make_row("b")], "hasMore": True, "cursor": 111}),
        FakeResponse({"data": [make_row("c")], "hasMore": False, "cursor": 222}),
    )

    results = search(client, take=2)

    assert len(results) == 9  # 3 rows x 3 available cabins
    first, second = (r["params"] for r in session.requests)
    assert first["skip"] == 0 and "cursor" not in first
    assert second["skip"] == 2 and second["cursor"] == 111


def test_search_raises_instead_of_truncating_when_max_pages_hit():
    endless = {"data": [make_row()], "hasMore": True, "cursor": 1}
    client, _ = make_client(FakeResponse(endless), FakeResponse(endless))

    with pytest.raises(SeatsAeroError, match="max_pages"):
        search(client, max_pages=2)


def test_search_stops_if_server_claims_more_but_returns_no_rows():
    client, session = make_client(FakeResponse({"data": [], "hasMore": True, "cursor": 1}))

    assert search(client) == []
    assert len(session.requests) == 1


def test_tracks_calls_remaining():
    client, _ = make_client(FakeResponse({"data": []}, remaining="997"))
    assert client.calls_remaining is None

    search(client)

    assert client.calls_remaining == 997


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_raises_auth_error(status):
    client, _ = make_client(FakeResponse(status_code=status))

    with pytest.raises(SeatsAeroAuthError):
        search(client)


def test_rate_limit_raises_and_records_zero_remaining():
    client, _ = make_client(FakeResponse(status_code=429, remaining="0"))

    with pytest.raises(SeatsAeroRateLimitError):
        search(client)
    assert client.calls_remaining == 0


def test_other_http_errors_raise_generic_error_with_status():
    client, _ = make_client(FakeResponse(status_code=500))

    with pytest.raises(SeatsAeroError) as excinfo:
        search(client)
    assert excinfo.value.status_code == 500
