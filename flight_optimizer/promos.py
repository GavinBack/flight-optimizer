"""Buy-miles sales, read from public deal-site feeds and roundup pages.

Sources (chosen after testing, 2026-09-20): Live From A Lounge, whose feed has structured
titles ("Deal: Buy Aeroplan miles with up to 100% Bonus through September 25, 2026") and
a weekly roundup page listing every current offer with its purchase tiers; and Doctor of
Credit's main feed. Every feed holds only the last few days, so this is meant to run daily
and remember what it has seen. One feed request per site per refresh, at most a couple of
page requests, and an identifying User-Agent: be a polite reader.

Promos are prose, not data. What can be read reliably:
- the program, the headline "up to N%", and the end date, from titles and roundup headings;
- purchase tiers ("Buy 5,000-25,000 points and get a 50% bonus") where a page lists them.
A promo without tiers is "headline only": its real bonus for a small purchase is unknown
(bonuses grow with purchase size), so it is shown as a best case and never alerted on.
"""

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests

USER_AGENT = "flight-optimizer/0.1 (personal use; reads deal feeds once a day)"
FEEDS = {
    "Live From A Lounge": "https://livefromalounge.com/feed/",
    "Doctor of Credit": "https://www.doctorofcredit.com/feed/",
    "LoyaltyLobby": "https://loyaltylobby.com/feed/",  # titles like "Delta Buy SkyMiles At Up To 50% Bonus Until ..."
}
MIN_REFRESH_INTERVAL = timedelta(hours=6)
UNDATED_PROMO_DAYS = 7  # a promo with no end date is treated as live while it keeps being seen

# Specific phrases first. Hotel programs are deliberately absent: they are not identified,
# so their posts are ignored.
_PROGRAM_WORDS = (
    ("virgin atlantic", "virginatlantic"), ("flying club", "virginatlantic"), ("virgin red", "virginatlantic"),
    ("virgin points", "virginatlantic"), ("flying blue", "flyingblue"), ("british airways", "british"),
    ("avios", "british"), ("lifemiles", "lifemiles"), ("avianca", "lifemiles"), ("aeroplan", "aeroplan"),
    ("air canada", "aeroplan"), ("skymiles", "delta"), ("delta", "delta"), ("aadvantage", "american"),
    ("american airlines", "american"), ("mileageplus", "united"), ("united", "united"),
    ("trueblue", "jetblue"), ("jetblue", "jetblue"), ("atmos", "atmos"), ("alaska", "atmos"),
    ("qantas", "qantas"), ("southwest", "southwest"), ("rapid rewards", "southwest"),
)

_MONTHS = {
    name: number
    for number, names in enumerate(
        (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
         ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
         ("oct", "october"), ("nov", "november"), ("dec", "december")), start=1)
    for name in names
}


class PromoSourceError(Exception):
    pass


@dataclass(frozen=True)
class PromoTier:
    min_points: int
    max_points: int | None  # None: no upper limit ("80,000 or more")
    percent: int

    def covers(self, points: int) -> bool:
        return points >= self.min_points and (self.max_points is None or points <= self.max_points)


@dataclass(frozen=True)
class Promo:
    key: str  # unique per source page and program
    program: str  # a balance key, e.g. "delta"
    kind: str  # "bonus" (extra points on top) or "discount" (lower price)
    headline_percent: int | None  # the "up to N%"
    tiers: tuple[PromoTier, ...]  # empty when only the headline is known
    ends: date | None
    title: str
    url: str
    source: str
    last_seen: date

    @property
    def tiers_known(self) -> bool:
        return bool(self.tiers)

    def is_active(self, today: date) -> bool:
        if self.ends is not None:
            return self.ends >= today
        return (today - self.last_seen).days <= UNDATED_PROMO_DAYS

    def percent_for(self, points: int) -> int:
        """The bonus or discount percent a purchase of `points` earns. With no tiers, the
        headline (a best case). Below the lowest tier, nothing."""
        if not self.tiers:
            return self.headline_percent or 0
        for tier in self.tiers:
            if tier.covers(points):
                return tier.percent
        top = max(self.tiers, key=lambda t: t.min_points)
        return top.percent if top.max_points is None and points >= top.min_points else 0


# ---------------------------------------------------------------------------- reading text

def program_in(text: str) -> str | None:
    """Which tracked program a headline is about, or None (hotels, cards, banks)."""
    lowered = text.lower()
    if "american express" in lowered and "aadvantage" not in lowered:
        lowered = lowered.replace("american express", " ")  # a card, not the airline
    for phrase, program in _PROGRAM_WORDS:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            return program
    return None


def is_buy_headline(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"\b(buy|buying|purchase|purchased|sells?|selling)\b", lowered)) and bool(
        re.search(r"\b(miles|points|avios|skymiles)\b", lowered)
    )


def parse_date(text: str, reference: date) -> date | None:
    """First 'Month d[, yyyy]' in the text. With no year, the next occurrence near `reference`."""
    for match in re.finditer(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text):
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            continue
        year = int(match.group(3)) if match.group(3) else reference.year
        try:
            found = date(year, month, int(match.group(2)))
        except ValueError:
            continue
        if not match.group(3) and found < reference - timedelta(days=60):
            found = found.replace(year=year + 1)
        return found
    return None


def parse_end_date(text: str, reference: date) -> date | None:
    match = re.search(
        r"\b(?:through|until|till|thru|ends?|expires?)\b\s*(?:on\s+)?(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
        r"([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)",
        text, re.I,
    )
    return parse_date(match.group(1), reference) if match else None


def parse_headline(text: str) -> tuple[str, int] | None:
    """('bonus' | 'discount', percent) from a title or heading. A bonus wins if both appear
    (United's "100% bonus or 50% discount" prices identically either way)."""
    bonus = re.search(r"(\d{2,3})\s*%\s*(?:transfer\s+)?bonus", text, re.I)
    if bonus:
        return "bonus", int(bonus.group(1))
    discount = re.search(r"(\d{2,3})\s*%\s*(?:discount|off)\b", text, re.I)
    return ("discount", int(discount.group(1))) if discount else None


_NUMBER = r"([\d,]+)"
_TIER_RANGE = re.compile(
    rf"(?:buy|purchase)\s+{_NUMBER}\s*(?:-|–|—|to)\s*{_NUMBER}\s+(?:miles|points|avios)\b[^%\n]{{0,60}}?(\d{{2,3}})\s*%\s*(bonus|discount|off)",
    re.I,
)
_TIER_OPEN = re.compile(
    rf"(?:buy|purchase)\s+{_NUMBER}\s*(?:\+|or more)\s+(?:miles|points|avios)\b[^%\n]{{0,60}}?(\d{{2,3}})\s*%\s*(bonus|discount|off)",
    re.I,
)


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def parse_tiers(text: str, kind: str) -> tuple[PromoTier, ...]:
    """Purchase tiers of the given kind, e.g. 'Buy 5,000-25,000 points and get a 50% bonus'."""
    wanted = ("bonus",) if kind == "bonus" else ("discount", "off")
    tiers: dict[int, PromoTier] = {}
    for match in _TIER_RANGE.finditer(text):
        if match.group(4).lower() in wanted:
            low, high = _to_int(match.group(1)), _to_int(match.group(2))
            tiers[low] = PromoTier(low, high, int(match.group(3)))
    for match in _TIER_OPEN.finditer(text):
        if match.group(3).lower() in wanted:
            low = _to_int(match.group(1))
            tiers[low] = PromoTier(low, None, int(match.group(2)))
    return tuple(sorted(tiers.values(), key=lambda t: t.min_points))


def plain_text(raw_html: str) -> str:
    """Readable text from a page, one block per line."""
    raw = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>", " ", raw_html, flags=re.S)
    raw = re.sub(r"<(br|/p|/li|/h\d|/tr|/div)[^>]*>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(line for line in (re.sub(r"\s+", " ", l).strip() for l in text.split("\n")) if line)


_ROUNDUP_HEADING = re.compile(
    r"^(?P<who>.+?)\s+is offering\s+(?:up to\s+)?(?:an?\s+)?(?P<pct>\d{2,3})\s*%\s*(?P<kind>bonus|discount)\b.*?"
    r"\((?:through|until)\s+(?P<end>[^)]+)\)",
    re.I,
)


def parse_roundup(text: str, url: str, today: date, source: str = "Live From A Lounge") -> list[Promo]:
    """Every tracked airline's promo in a roundup page, with tiers where the page lists them."""
    found: dict[str, dict] = {}
    current: str | None = None
    for line in text.split("\n"):
        heading = _ROUNDUP_HEADING.match(line)
        if heading:
            program = program_in(heading.group("who"))
            current = program
            if program and program not in found:
                found[program] = {
                    "kind": heading.group("kind").lower(), "percent": int(heading.group("pct")),
                    "ends": parse_date(heading.group("end"), today), "title": line, "lines": [],
                }
            continue
        if current and current in found:
            found[current]["lines"].append(line)
    promos = []
    for program, info in found.items():
        tiers = parse_tiers("\n".join(info["lines"]), info["kind"])
        promos.append(Promo(
            key=f"{url}#{program}", program=program, kind=info["kind"], headline_percent=info["percent"],
            tiers=tiers, ends=info["ends"], title=info["title"][:200], url=url, source=source, last_seen=today,
        ))
    return promos


def promo_from_title(title: str, url: str, published: date, source: str, today: date) -> Promo | None:
    """A headline-only promo from a post title like 'Deal: Buy X miles with up to N% Bonus through DATE'."""
    if title.lstrip().lower().startswith("[expired]") or not is_buy_headline(title):
        return None
    program = program_in(title)
    headline = parse_headline(title)
    if program is None or headline is None:
        return None
    ends = parse_end_date(title, published)
    if ends is not None and ends < today:
        return None
    return Promo(
        key=url, program=program, kind=headline[0], headline_percent=headline[1], tiers=(), ends=ends,
        title=title[:200], url=url, source=source, last_seen=today,
    )


def best_promos(promos: list[Promo]) -> dict[str, Promo]:
    """One promo per program: tiers known beats headline only, then the bigger headline."""
    best: dict[str, Promo] = {}
    for promo in promos:
        rank = (promo.tiers_known, promo.headline_percent or 0)
        if promo.program not in best or rank > (best[promo.program].tiers_known, best[promo.program].headline_percent or 0):
            best[promo.program] = promo
    return best


# ---------------------------------------------------------------------------- network

@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str
    published: date


class PromoSource:
    """Reads feeds and pages over HTTP. Errors never carry more than the host name."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 30):
        self._session = session or requests.Session()
        self._timeout = timeout

    def _get(self, url: str) -> requests.Response:
        host = urlparse(url).netloc
        try:
            response = self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=self._timeout)
        except requests.RequestException as exc:
            raise PromoSourceError(f"could not reach {host} ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise PromoSourceError(f"{host} returned HTTP {response.status_code}")
        return response

    def feed(self, url: str) -> list[FeedEntry]:
        parsed = feedparser.parse(self._get(url).content)
        entries = []
        for entry in parsed.entries:
            when = entry.get("published_parsed")
            entries.append(FeedEntry(
                title=entry.get("title", ""), link=entry.get("link", ""),
                published=date(when.tm_year, when.tm_mon, when.tm_mday) if when else date.today(),
            ))
        return entries

    def page_text(self, url: str) -> str:
        return plain_text(self._get(url).text)


# ---------------------------------------------------------------------------- storage

def _row_to_promo(row: sqlite3.Row) -> Promo:
    import json
    return Promo(
        key=row["key"], program=row["program"], kind=row["kind"], headline_percent=row["headline_percent"],
        tiers=tuple(PromoTier(t[0], t[1], t[2]) for t in json.loads(row["tiers_json"])),
        ends=date.fromisoformat(row["ends"]) if row["ends"] else None, title=row["title"], url=row["url"],
        source=row["source"], last_seen=date.fromisoformat(row["last_seen"]),
    )


def save_promo(conn: sqlite3.Connection, promo: Promo) -> None:
    """Insert or refresh a promo. A promo already stored with tiers keeps them if the new
    sighting has none (a title-only sighting must not erase what a roundup taught us)."""
    import json
    tiers = promo.tiers
    existing = conn.execute("SELECT tiers_json FROM promos WHERE key = ?", (promo.key,)).fetchone()
    if not tiers and existing is not None:
        tiers = tuple(PromoTier(t[0], t[1], t[2]) for t in json.loads(existing["tiers_json"]))
    with conn:
        conn.execute(
            "INSERT INTO promos (key, program, kind, headline_percent, tiers_json, ends, title, url, source,"
            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, headline_percent = excluded.headline_percent,"
            " tiers_json = excluded.tiers_json, ends = excluded.ends, title = excluded.title,"
            " last_seen = excluded.last_seen",
            (promo.key, promo.program, promo.kind, promo.headline_percent,
             json.dumps([[t.min_points, t.max_points, t.percent] for t in tiers]),
             promo.ends.isoformat() if promo.ends else None, promo.title, promo.url, promo.source,
             promo.last_seen.isoformat(), promo.last_seen.isoformat()),
        )


def active_promos(conn: sqlite3.Connection, today: date) -> list[Promo]:
    return [p for p in (_row_to_promo(r) for r in conn.execute("SELECT * FROM promos")) if p.is_active(today)]


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


@dataclass
class RefreshResult:
    skipped: bool = False  # refreshed too recently, nothing fetched
    new_or_updated: int = 0
    pages_read: int = 0
    errors: tuple[str, ...] = ()


def refresh_promos(
    conn: sqlite3.Connection, source: PromoSource, now: datetime, *, force: bool = False,
    min_interval: timedelta = MIN_REFRESH_INTERVAL,
) -> RefreshResult:
    """Read the feeds and the newest roundup page, and store what they say.

    A feed that can't be reached is reported, not fatal: the other source still runs.
    """
    last = _meta_get(conn, "promos:last_refresh")
    if not force and last is not None:
        age = now.astimezone(timezone.utc) - datetime.fromisoformat(last)
        if age < min_interval:
            return RefreshResult(skipped=True)

    today = now.date()
    result = RefreshResult()
    errors: list[str] = []
    for name, url in FEEDS.items():
        try:
            entries = source.feed(url)
        except PromoSourceError as exc:
            errors.append(f"{name}: {exc}")
            continue
        for entry in entries:
            if name == "Live From A Lounge" and re.search(r"buying opportunities", entry.title, re.I):
                if _meta_get(conn, f"promos:page:{entry.link}") is None:
                    try:
                        promos = parse_roundup(source.page_text(entry.link), entry.link, today, name)
                    except PromoSourceError as exc:
                        errors.append(f"{name} roundup: {exc}")
                        continue
                    for promo in promos:
                        save_promo(conn, promo)
                    _meta_set(conn, f"promos:page:{entry.link}", today.isoformat())
                    result.pages_read += 1
                    result.new_or_updated += len(promos)
                continue
            promo = promo_from_title(entry.title, entry.link, entry.published, name, today)
            if promo is not None:
                save_promo(conn, promo)
                result.new_or_updated += 1
    _meta_set(conn, "promos:last_refresh", now.astimezone(timezone.utc).isoformat())
    result.errors = tuple(errors)
    return result
