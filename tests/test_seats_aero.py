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


# --- trips lookup (flight-level details) -----------------------------------------

from datetime import datetime  # noqa: E402

from flight_optimizer.seats_aero import parse_trips  # noqa: E402


def trips_body():
    """Trimmed from a real /trips response (LAX-SJO on Aeroplan)."""
    return {
        "data": [
            {"Cabin": "business", "MileageCost": 25000, "RemainingSeats": 5, "Stops": 0,
             "DepartsAt": "2027-01-09T13:40:00Z", "ArrivesAt": "2027-01-09T23:15:00Z",
             "FlightNumbers": "AV641", "TotalDuration": 455, "TotalTaxes": 10600,
             "TaxesCurrency": "CAD", "UpdatedAt": "2026-09-18T01:33:05Z"},
            {"Cabin": "business", "MileageCost": "25000", "RemainingSeats": 2, "Stops": 1,
             "DepartsAt": "2027-01-09T13:40:00Z", "ArrivesAt": "2027-01-10T08:35:00Z",
             "FlightNumbers": "AV641, AV655", "TotalDuration": 1015, "TotalTaxes": 10970,
             "TaxesCurrency": "CAD"},
            {"Cabin": "economy", "MileageCost": 15000, "RemainingSeats": 9, "Stops": 0,
             "DepartsAt": "2027-01-09T13:40:00Z", "ArrivesAt": "2027-01-09T23:15:00Z"},
            {"Cabin": "lounge", "MileageCost": 1, "DepartsAt": "2027-01-09T13:40:00Z", "ArrivesAt": "2027-01-09T23:15:00Z"},
            {"Cabin": "business", "MileageCost": 0, "DepartsAt": "2027-01-09T13:40:00Z", "ArrivesAt": "2027-01-09T23:15:00Z"},
            {"Cabin": "business", "MileageCost": 9000, "ArrivesAt": "2027-01-09T23:15:00Z"},
        ],
        "booking_links": [
            {"label": "Book via United MileagePlus", "link": "https://united.test/x", "primary": False},
            {"label": "Book via Air Canada Aeroplan", "link": "https://aircanada.test/x", "primary": True},
        ],
    }


def test_parse_trips_reads_flights_and_skips_unusable_entries():
    details = parse_trips(trips_body())

    assert [(o.cabin, o.miles, o.seats, o.stops) for o in details.options] == [
        (Cabin.BUSINESS, 25_000, 5, 0),
        (Cabin.BUSINESS, 25_000, 2, 1),   # string cost is converted
        (Cabin.ECONOMY, 15_000, 9, 0),
    ]  # unknown cabin, zero cost and missing time are all skipped
    first = details.options[0]
    assert first.flight_numbers == "AV641" and first.total_taxes == 10_600 and first.currency == "CAD"


def test_parse_trips_times_are_naive_local_times_despite_the_z_suffix():
    first = parse_trips(trips_body()).options[0]

    assert first.departs_at == datetime(2027, 1, 9, 13, 40) and first.departs_at.tzinfo is None
    assert first.arrives_at == datetime(2027, 1, 9, 23, 15)


def test_parse_trips_uses_the_primary_booking_link():
    details = parse_trips(trips_body())

    assert details.booking_url == "https://aircanada.test/x"
    assert ("Book via United MileagePlus", "https://united.test/x") in details.booking_links


def test_parse_trips_without_links_or_data_is_empty_not_an_error():
    details = parse_trips({})

    assert details.options == () and details.booking_url == "" and details.booking_links == ()


def test_client_trips_calls_the_trips_endpoint_with_the_row_id():
    client, session = make_client(FakeResponse(trips_body()))

    details = client.trips("abc123")

    assert session.requests[0]["url"] == "https://seats.aero/partnerapi/trips/abc123"
    assert session.requests[0]["headers"] == {"Partner-Authorization": "test-key"}
    assert len(details.options) == 3


def test_parse_availability_keeps_the_row_id_for_later_lookups():
    assert parse_availability(make_row("row-42"))[0].availability_id == "row-42"


def test_calls_made_counts_every_api_call_including_failed_ones():
    client, _ = make_client(FakeResponse({"data": []}), FakeResponse(trips_body()), FakeResponse(status_code=500))

    assert client.calls_made == 0
    search(client)
    client.trips("abc", version="ignored-by-the-raw-client")
    with pytest.raises(SeatsAeroError):
        search(client)

    assert client.calls_made == 3


def test_trips_results_from_the_api_are_not_marked_as_cached():
    client, _ = make_client(FakeResponse(trips_body()))

    assert client.trips("abc").from_cache is False
