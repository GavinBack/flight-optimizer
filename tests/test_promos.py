from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from flight_optimizer import db
from flight_optimizer.promos import (
    FEEDS, FeedEntry, Promo, PromoSource, PromoSourceError, PromoTier, active_promos, best_promos,
    is_buy_headline, parse_date, parse_end_date, parse_headline, parse_roundup, parse_tiers,
    plain_text, program_in, promo_from_title, refresh_promos, save_promo,
)

TODAY = date(2026, 9, 20)


# --- recognizing programs and buy headlines -------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Avianca LifeMiles is offering up to a 145% bonus", "lifemiles"),
    ("Buy Aeroplan miles with up to 100% Bonus", "aeroplan"),
    ("Air Canada Aeroplan", "aeroplan"),
    ("Delta Buy SkyMiles At Up To 50% Bonus", "delta"),
    ("American Airlines AAdvantage is offering a discount", "american"),
    ("United MileagePlus is offering", "united"),
    ("JetBlue TrueBlue points", "jetblue"),
    ("Alaska Atmos Rewards", "atmos"),
    ("Virgin Atlantic Flying Club", "virginatlantic"),
    ("Flying Blue miles", "flyingblue"),
    ("British Airways Avios", "british"),
])
def test_program_in_recognizes_the_tracked_programs(text, expected):
    assert program_in(text) == expected


def test_a_credit_card_is_not_mistaken_for_the_airline():
    assert program_in("American Express Delta Offers: Up To 90,000 Miles") == "delta"   # the Delta part is real
    assert program_in("American Express Platinum 175,000 Points Signup Bonus") is None


@pytest.mark.parametrize("text", ["IHG Points 100% bonus", "Marriott Bonvoy sale", "Chase Freedom Flex", "Lufthansa Miles & More"])
def test_hotels_banks_and_untracked_programs_are_ignored(text):
    assert program_in(text) is None


@pytest.mark.parametrize("text, expected", [
    ("Deal: Buy Aeroplan miles with up to 100% Bonus", True),
    ("Purchase Miles From 1.82¢ Per Mile", True),
    ("Delta Buy SkyMiles At Up To 50% Bonus", True),
    ("American Airlines is selling miles at a discount", True),
    ("Chase Sapphire signup bonus points", False),
    ("Citi adds Japan Airlines as a partner, launches 30% transfer bonus", False),
])
def test_is_buy_headline(text, expected):
    assert is_buy_headline(text) is expected


# --- dates ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("September 25, 2026", date(2026, 9, 25)),
    ("September 25 2026", date(2026, 9, 25)),
    ("Sept. 30, 2026", date(2026, 9, 30)),
    ("Oct 1st, 2026", date(2026, 10, 1)),
    ("October 1", date(2026, 10, 1)),                  # no year: near the reference date
    ("January 5", date(2027, 1, 5)),                   # already well past this year: next year
    ("nothing here", None),
    ("Februrary 30, 2026", None),                      # not a month, or not a real day
])
def test_parse_date(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text, expected", [
    ("Deal: Buy Aeroplan miles with up to 100% Bonus through September 25, 2026", date(2026, 9, 25)),
    ("Delta Buy SkyMiles At Up To 50% Bonus Until September 30, 2026", date(2026, 9, 30)),
    ("Through Friday, September 25, 2026, Avianca LifeMiles is offering", date(2026, 9, 25)),
    ("Buy JetBlue points [Ends October 1]", date(2026, 10, 1)),
    ("Buy Aeroplan miles with a bonus through September 25 2026", date(2026, 9, 25)),
    ("Aeroplan: Buy Miles & Get Up To 115% Bonus", None),
])
def test_parse_end_date(text, expected):
    assert parse_end_date(text, TODAY) == expected


# --- headlines and tiers ------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Buy miles with up to 100% Bonus", ("bonus", 100)),
    ("American Airlines up to a 40% discount on purchased miles", ("discount", 40)),
    ("Sale: 30% off miles", ("discount", 30)),
    ("United is offering up to a 100% bonus or 50% discount", ("bonus", 100)),   # a bonus wins
    ("Purchase miles From 1.82¢ Per Mile", None),
])
def test_parse_headline(text, expected):
    assert parse_headline(text) == expected


AEROPLAN_TIERS = """
Buy 5,000-25,000 points and get a 50% bonus
Buy 30,000-75,000 points and get a 75% bonus
Buy 80,000 or more points and get a 100% bonus
"""


def test_parse_tiers_reads_ranges_and_an_open_ended_top_tier():
    tiers = parse_tiers(AEROPLAN_TIERS, "bonus")

    assert tiers == (PromoTier(5_000, 25_000, 50), PromoTier(30_000, 75_000, 75), PromoTier(80_000, None, 100))


def test_parse_tiers_handles_dashes_bullets_and_purchase_wording():
    text = "– Buy 3,000–9,000 points and get a 100% bonus\nPurchase 10,000 — 19,000 miles and get a 110% bonus (1.2¢ per mile)"

    assert parse_tiers(text, "bonus") == (PromoTier(3_000, 9_000, 100), PromoTier(10_000, 19_000, 110))


def test_parse_tiers_only_returns_the_requested_kind():
    text = "Buy 5,000-10,000 miles and get a 40% discount\nBuy 5,000-10,000 miles and get a 50% bonus"

    assert parse_tiers(text, "bonus") == (PromoTier(5_000, 10_000, 50),)
    assert parse_tiers(text, "discount") == (PromoTier(5_000, 10_000, 40),)


def test_parse_tiers_returns_nothing_for_prose_without_a_table():
    assert parse_tiers("Delta is offering up to a 50% bonus on purchased miles", "bonus") == ()


def test_tiers_are_sorted_and_deduplicated():
    text = "Buy 30,000-75,000 points and get a 75% bonus\nBuy 5,000-25,000 points and get a 50% bonus\nBuy 5,000-25,000 points and get a 50% bonus"

    assert [t.min_points for t in parse_tiers(text, "bonus")] == [5_000, 30_000]


def promo(**overrides):
    fields = dict(
        key="k", program="aeroplan", kind="bonus", headline_percent=100,
        tiers=parse_tiers(AEROPLAN_TIERS, "bonus"), ends=date(2026, 9, 25), title="t", url="https://x.test/p",
        source="test", last_seen=TODAY,
    )
    return Promo(**{**fields, **overrides})


@pytest.mark.parametrize("points, percent", [
    (2_000, 0),       # below the lowest tier
    (5_000, 50), (25_000, 50),
    (26_000, 0),      # the gap between tiers
    (30_000, 75),
    (80_000, 100), (500_000, 100),   # the open-ended top tier
])
def test_the_percent_depends_on_the_size_of_the_purchase(points, percent):
    assert promo().percent_for(points) == percent


def test_a_promo_without_tiers_uses_the_headline_as_a_best_case():
    headline_only = promo(tiers=())

    assert not headline_only.tiers_known and headline_only.percent_for(3_000) == 100


def test_a_promo_is_active_until_its_end_date_inclusive():
    assert promo(ends=date(2026, 9, 20)).is_active(TODAY)
    assert not promo(ends=date(2026, 9, 19)).is_active(TODAY)


def test_an_undated_promo_is_live_only_while_it_keeps_being_seen():
    undated = promo(ends=None, last_seen=TODAY - timedelta(days=7))

    assert undated.is_active(TODAY)
    assert not promo(ends=None, last_seen=TODAY - timedelta(days=8)).is_active(TODAY)


# --- the roundup page ------------------------------------------------------------------------------------

ROUNDUP = """
Airline Miles
Avianca LifeMiles is offering up to a 145% bonus on purchased miles (Through September 25, 2026)
Through Friday, September 25, 2026, Avianca LifeMiles is offering a flash sale. The structure is as follows:
Buy 1,000-20,000 miles and get a 125% bonus
Buy 21,000-50,000 miles and get a 135% bonus
Buy 51,000-200,000 miles and get a 145% bonus
Air Canada Aeroplan is offering up to a 100% bonus on purchased points (Through September 25, 2026)
Buy 5,000-25,000 points and get a 50% bonus
Buy 30,000-75,000 points and get a 75% bonus
Buy 80,000 or more points and get a 100% bonus
Lufthansa Miles & More is offering a 50% bonus on Bundle&Go packages (Through September 30, 2026)
Buy 1,000-5,000 miles and get a 99% bonus
United MileagePlus is offering up to a 100% bonus or 50% discount on purchased miles (Through September 30, 2026)
American AAdvantage is offering up to a 40% discount on purchased miles (Through September 30, 2026)
JetBlue TrueBlue is offering up to a 120% bonus on purchased points (Through October 1, 2026)
– Buy 3,000-9,000 points and get a 100% bonus
– Buy 10,000-19,000 points and get a 110% bonus
United MileagePlus is offering up to a 100% bonus or 50% discount on purchased miles (Through September 30, 2026)
"""
URL = "https://livefromalounge.com/roundup/"


def roundup():
    return {p.program: p for p in parse_roundup(ROUNDUP, URL, TODAY)}


def test_the_roundup_yields_every_tracked_airline_and_only_those():
    assert set(roundup()) == {"lifemiles", "aeroplan", "united", "american", "jetblue"}


def test_roundup_tiers_are_attached_to_the_right_program():
    found = roundup()

    assert [t.percent for t in found["lifemiles"].tiers] == [125, 135, 145]
    assert [t.percent for t in found["aeroplan"].tiers] == [50, 75, 100]
    assert [t.percent for t in found["jetblue"].tiers] == [100, 110]


def test_tiers_never_leak_from_an_untracked_program_into_a_neighbor():
    found = roundup()

    assert all(t.percent != 99 for p in found.values() for t in p.tiers)   # Lufthansa's tier
    assert found["aeroplan"].tiers[-1] == PromoTier(80_000, None, 100)     # not extended by Lufthansa's line


def test_headline_only_programs_are_marked_as_such():
    found = roundup()

    assert not found["united"].tiers_known and not found["american"].tiers_known
    assert found["american"].kind == "discount" and found["american"].headline_percent == 40
    assert found["united"].kind == "bonus" and found["united"].headline_percent == 100


def test_roundup_end_dates_and_keys():
    found = roundup()

    assert found["aeroplan"].ends == date(2026, 9, 25) and found["jetblue"].ends == date(2026, 10, 1)
    assert found["aeroplan"].key == f"{URL}#aeroplan" and found["aeroplan"].url == URL


def test_a_program_listed_twice_is_kept_once():
    assert len([p for p in parse_roundup(ROUNDUP, URL, TODAY) if p.program == "united"]) == 1


def test_a_page_with_no_offers_yields_nothing():
    assert parse_roundup("Just some text\nabout travel", URL, TODAY) == []


# --- titles ----------------------------------------------------------------------------------------------------

def title_promo(title, published=TODAY):
    return promo_from_title(title, "https://x.test/post", published, "test", TODAY)


def test_a_live_from_a_lounge_deal_title():
    found = title_promo("Deal: Buy Aeroplan miles with up to 100% Bonus through September 25, 2026")

    assert (found.program, found.kind, found.headline_percent, found.ends) == ("aeroplan", "bonus", 100, date(2026, 9, 25))
    assert not found.tiers_known


def test_a_loyaltylobby_title_for_delta():
    found = title_promo("Delta Buy SkyMiles At Up To 50% Bonus Until September 30, 2026")

    assert (found.program, found.kind, found.headline_percent, found.ends) == ("delta", "bonus", 50, date(2026, 9, 30))


def test_a_doctor_of_credit_title_has_no_end_date():
    found = title_promo("Aeroplan: Buy Miles & Get Up To 115% Bonus")

    assert found.headline_percent == 115 and found.ends is None


@pytest.mark.parametrize("title", [
    "[Expired] Avianca LifeMiles: Buy Miles From 1.1¢ & 20% Off Partner Redemptions",
    "American Express Delta Offers – Up To 90,000 Miles + $300",
    "Chase Sapphire Reserve 100,000 Points Signup Bonus",
    "IHG Points: Buy Points & Get A 100% Bonus",
    "Deal: Buy Aeroplan miles with up to 100% Bonus through September 1, 2026",   # already over
    "Purchase Miles From 1.82¢ Per Mile",                                         # no program named
])
def test_titles_that_are_not_live_tracked_airline_sales_are_ignored(title):
    assert title_promo(title) is None


def test_best_promos_prefers_known_tiers_then_the_bigger_headline():
    with_tiers = promo(key="a", headline_percent=100)
    headline_only_bigger = promo(key="b", tiers=(), headline_percent=150)
    smaller = promo(key="c", program="delta", tiers=(), headline_percent=30)
    bigger = promo(key="d", program="delta", tiers=(), headline_percent=50)

    best = best_promos([headline_only_bigger, with_tiers, smaller, bigger])

    assert best["aeroplan"].key == "a" and best["delta"].key == "d"


# --- storage ----------------------------------------------------------------------------------------------------

@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def test_a_promo_survives_a_round_trip_through_the_database(conn):
    original = promo()

    save_promo(conn, original)

    assert active_promos(conn, TODAY) == [original]


def test_expired_promos_are_not_returned(conn):
    save_promo(conn, promo(key="old", ends=date(2026, 9, 1)))
    save_promo(conn, promo(key="new"))

    assert [p.key for p in active_promos(conn, TODAY)] == ["new"]


def test_a_title_only_sighting_does_not_erase_tiers_learned_from_a_roundup(conn):
    save_promo(conn, promo())
    save_promo(conn, promo(tiers=(), headline_percent=100))   # the same promo, seen again as a bare title

    (stored,) = active_promos(conn, TODAY)
    assert stored.tiers_known and len(stored.tiers) == 3


def test_seeing_a_promo_again_updates_it_and_keeps_the_first_seen_date(conn):
    save_promo(conn, promo(last_seen=date(2026, 9, 18)))
    save_promo(conn, promo(last_seen=date(2026, 9, 20), ends=date(2026, 9, 27)))

    (stored,) = active_promos(conn, TODAY)
    assert stored.ends == date(2026, 9, 27) and stored.last_seen == date(2026, 9, 20)
    assert conn.execute("SELECT first_seen FROM promos").fetchone()["first_seen"] == "2026-09-18"


# --- refreshing from the network (with a fake source) -------------------------------------------------

class FakeSource:
    def __init__(self, feeds=None, pages=None, fail=()):
        self.feeds, self.pages, self.fail = feeds or {}, pages or {}, set(fail)
        self.feed_calls, self.page_calls = [], []

    def feed(self, url):
        self.feed_calls.append(url)
        if url in self.fail:
            raise PromoSourceError("boom")
        return self.feeds.get(url, [])

    def page_text(self, url):
        self.page_calls.append(url)
        return self.pages[url]


LFAL, DOC, LOYALTY = FEEDS["Live From A Lounge"], FEEDS["Doctor of Credit"], FEEDS["LoyaltyLobby"]
NOW = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
ROUNDUP_ENTRY = FeedEntry("15 great miles and points buying opportunities available as of September 20, 2026", URL, TODAY)


def test_a_refresh_reads_the_newest_roundup_and_the_deal_titles(conn):
    source = FakeSource(
        feeds={LFAL: [ROUNDUP_ENTRY], LOYALTY: [FeedEntry("Delta Buy SkyMiles At Up To 50% Bonus Until September 30, 2026", "https://ll.test/delta", TODAY)]},
        pages={URL: ROUNDUP},
    )

    result = refresh_promos(conn, source, NOW)

    programs = {p.program for p in active_promos(conn, TODAY)}
    assert programs == {"lifemiles", "aeroplan", "united", "american", "jetblue", "delta"}
    assert result.pages_read == 1 and result.new_or_updated == 6 and not result.errors


def test_the_same_roundup_page_is_never_read_twice(conn):
    source = FakeSource(feeds={LFAL: [ROUNDUP_ENTRY]}, pages={URL: ROUNDUP})
    refresh_promos(conn, source, NOW)

    refresh_promos(conn, source, NOW + timedelta(days=1))

    assert source.page_calls == [URL]


def test_a_refresh_within_six_hours_touches_nothing(conn):
    source = FakeSource(feeds={LFAL: [ROUNDUP_ENTRY]}, pages={URL: ROUNDUP})
    refresh_promos(conn, source, NOW)
    calls = len(source.feed_calls)

    result = refresh_promos(conn, source, NOW + timedelta(hours=5, minutes=59))

    assert result.skipped and len(source.feed_calls) == calls


def test_a_refresh_after_six_hours_or_forced_runs_again(conn):
    source = FakeSource(feeds={LFAL: []})
    refresh_promos(conn, source, NOW)

    assert not refresh_promos(conn, source, NOW + timedelta(hours=6, minutes=1)).skipped
    assert not refresh_promos(conn, source, NOW + timedelta(hours=6, minutes=2), force=True).skipped


def test_one_broken_feed_does_not_stop_the_others(conn):
    source = FakeSource(
        feeds={LOYALTY: [FeedEntry("Delta Buy SkyMiles At Up To 50% Bonus Until September 30, 2026", "https://ll.test/d", TODAY)]},
        fail=[LFAL],
    )

    result = refresh_promos(conn, source, NOW)

    assert [p.program for p in active_promos(conn, TODAY)] == ["delta"]
    assert len(result.errors) == 1 and "Live From A Lounge" in result.errors[0] and "boom" in result.errors[0]


def test_a_failed_roundup_page_is_reported_and_retried_next_time(conn):
    class FlakyPage(FakeSource):
        def page_text(self, url):
            self.page_calls.append(url)
            raise PromoSourceError("page down")

    source = FlakyPage(feeds={LFAL: [ROUNDUP_ENTRY]})
    result = refresh_promos(conn, source, NOW)
    assert result.errors and result.pages_read == 0

    working = FakeSource(feeds={LFAL: [ROUNDUP_ENTRY]}, pages={URL: ROUNDUP})
    assert refresh_promos(conn, working, NOW + timedelta(days=1)).pages_read == 1


def test_expired_and_unrelated_titles_are_not_stored(conn):
    source = FakeSource(feeds={DOC: [
        FeedEntry("[Expired] Aeroplan: Buy Miles From 1.26¢ Per Mile", "https://doc.test/1", TODAY),
        FeedEntry("[FL Only] Connect Credit Union $400 Checking Bonus", "https://doc.test/2", TODAY),
    ]})

    refresh_promos(conn, source, NOW)

    assert active_promos(conn, TODAY) == []


# --- the real network wrapper (with a fake HTTP session) ----------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Deal: Buy Aeroplan miles with up to 100% Bonus through September 25, 2026</title>
<link>https://x.test/1</link><pubDate>Tue, 15 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""


class FakeResponse:
    def __init__(self, content=b"", text="", status_code=200):
        self.content, self.text, self.status_code = content, text, status_code


class FakeSession:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def get(self, url, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_the_feed_reader_parses_titles_links_and_dates():
    session = FakeSession(FakeResponse(content=RSS))

    (entry,) = PromoSource(session).feed("https://x.test/feed")

    assert entry.title.startswith("Deal: Buy Aeroplan") and entry.link == "https://x.test/1"
    assert entry.published == date(2026, 9, 15)
    assert "flight-optimizer" in session.requests[0]["headers"]["User-Agent"]   # an identifying agent


def test_page_text_is_readable_text():
    html = "<html><body><nav>menu</nav><p>Buy 5,000-25,000 points and get a 50% bonus</p><script>x()</script></body></html>"

    text = PromoSource(FakeSession(FakeResponse(text=html))).page_text("https://x.test/p")

    assert "Buy 5,000-25,000 points and get a 50% bonus" in text and "menu" not in text and "x()" not in text


def test_http_errors_name_only_the_host():
    with pytest.raises(PromoSourceError) as excinfo:
        PromoSource(FakeSession(FakeResponse(status_code=403))).feed("https://loyaltylobby.com/?s=secret-query")

    assert "loyaltylobby.com returned HTTP 403" in str(excinfo.value) and "secret-query" not in str(excinfo.value)


def test_network_errors_are_wrapped_without_their_text():
    leaky = requests.ConnectionError("HTTPSConnectionPool(host='x.test'): failed with url /?token=abc123")

    with pytest.raises(PromoSourceError) as excinfo:
        PromoSource(FakeSession(leaky)).page_text("https://x.test/page?token=abc123")

    assert "abc123" not in str(excinfo.value) and "x.test" in str(excinfo.value)
    assert excinfo.value.__suppress_context__


def test_plain_text_puts_blocks_on_separate_lines():
    assert plain_text("<p>one</p><p>two</p><li>three</li>").split("\n") == ["one", "two", "three"]
