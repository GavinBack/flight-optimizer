"""Program names, and turning what a person types into a program key and a mileage."""

import re
from datetime import date, datetime

from flight_optimizer import db
from flight_optimizer.affordability import CAPITAL_ONE_TRANSFERS, ratio_text

PROGRAM_NAMES = {
    "capital_one": "Capital One Miles",
    "delta": "Delta SkyMiles",
    "american": "American AAdvantage",
    "united": "United MileagePlus",
    "qantas": "Qantas Frequent Flyer",
    "atmos": "Alaska/Hawaiian (Atmos Rewards)",
    "southwest": "Southwest Rapid Rewards",
    "aeroplan": "Air Canada Aeroplan",
    "lifemiles": "Avianca LifeMiles",
    "flyingblue": "Air France/KLM Flying Blue",
    "british": "British Airways Avios",
    "virginatlantic": "Virgin Atlantic Flying Club",
    "jetblue": "JetBlue TrueBlue",
}

# Things people call these programs. Normalized (lowercase letters and digits only).
ALIASES = {
    "c1": "capital_one", "capitalone": "capital_one", "venture": "capital_one",
    "aa": "american", "aadvantage": "american",
    "dl": "delta", "skymiles": "delta",
    "ua": "united", "mileageplus": "united",
    "alaska": "atmos", "hawaiian": "atmos",
    "avios": "british", "ba": "british",
    "airfrance": "flyingblue", "klm": "flyingblue",
    "avianca": "lifemiles",
    "aircanada": "aeroplan",
    "virginred": "virginatlantic", "flyingclub": "virginatlantic", "virgin": "virginatlantic",
}

STALE_AFTER_DAYS = 30  # a balance not touched for this long is flagged as possibly out of date

assert tuple(PROGRAM_NAMES) == db.PROGRAMS, "PROGRAM_NAMES must cover exactly the tracked programs"


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def resolve_program(text: str) -> str:
    """The program key for what was typed: a key, a name, an alias, or a unique prefix."""
    wanted = _normalize(text)
    if not wanted:
        raise ValueError("Program name is empty.")

    exact = {**{_normalize(key): key for key in PROGRAM_NAMES},
             **{_normalize(name): key for key, name in PROGRAM_NAMES.items()}, **ALIASES}
    if wanted in exact:
        return exact[wanted]

    matches = sorted({key for key in PROGRAM_NAMES if _normalize(key).startswith(wanted)})
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"{text!r} could mean any of: {', '.join(matches)}. Be more specific.")
    raise ValueError(f"Unknown program {text!r}. Valid programs: {', '.join(PROGRAM_NAMES)}.")


def parse_miles(text: str) -> int:
    """A whole, non-negative number of miles. Commas and underscores are allowed."""
    cleaned = text.strip().replace(",", "").replace("_", "")
    if not re.fullmatch(r"\d+", cleaned):
        raise ValueError(f"{text!r} is not a whole number of miles (use digits, e.g. 12000 or 12,000).")
    return int(cleaned)


def format_balances(balances: dict[str, int], updated: dict[str, str], *, today: date | None = None) -> str:
    today = today or date.today()
    rows = sorted(PROGRAM_NAMES, key=lambda key: (-balances.get(key, 0), PROGRAM_NAMES[key]))
    width = max(len(name) for name in PROGRAM_NAMES.values()) + 2
    lines = [f"{'Program':<{width}}{'Balance':>10}   {'Last set':<12}Notes"]
    for key in rows:
        notes = []
        if key == "capital_one":
            notes.append("transferable pool")
        if key in CAPITAL_ONE_TRANSFERS:
            notes.append(f"Capital One transfer {ratio_text(key)}")
        if key in updated:
            set_on = datetime.strptime(updated[key], "%Y-%m-%dT%H:%M:%SZ").date()
            when = set_on.isoformat()
            if (today - set_on).days > STALE_AFTER_DAYS:
                notes.append(f"not updated in {(today - set_on).days} days")
        else:
            when = "never set"
        lines.append(f"{PROGRAM_NAMES[key]:<{width}}{balances.get(key, 0):>10,}   {when:<12}{'; '.join(notes)}".rstrip())
    return "\n".join(lines)
