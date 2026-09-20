# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Early stage. Built so far: the SQLite layer, the Seats.aero client, config loading, and their tests. Not built: the scheduler, cash-fare client, rules engine, notifications, and buy-miles promo tracking. `README.md` is the product spec and its architecture section is explicitly a draft. `todo.md` tracks open decisions and the build order, so check it before assuming something has been decided.

Remote: `git@github.com:GavinBack/flight-optimizer.git` (SSH, private), branch `main`.

## What this is

A personal alerting tool (not a booking tool, and auto-booking is out of scope) that checks daily for the best way to book a trip, with cash or miles across several loyalty programs, and notifies the owner when something is worth booking.

## Commands

Python 3.12 in a local virtualenv:

```
python -m venv .venv                      # already created locally
source .venv/bin/activate
pip install -r requirements.txt
pytest                                    # all tests; none touch the network or spend API calls
pytest tests/test_db.py::test_name        # a single test
python -m flight_optimizer [--top N]    # live report for active trips; ~18 Seats.aero calls, cash prices come from the cache
```

No linter, formatter, or entry-point script has been chosen yet. Don't invent one. Add it to this file when it's introduced.

## Code layout

`flight_optimizer/`: `config.py` (env/.env settings), `db.py` (schema and helpers), `seats_aero.py` (API client), `itineraries.py` (gateway search: direct pair plus origin->hub and hub->destination, combined into itineraries), `affordability.py` (ranks trips by what balances can pay for), `cash_fares.py` (SerpApi client, cache, baseline rule), `savings.py` (cents per mile vs cash baseline), `report.py` (plain-text report), `__main__.py` (CLI). Tests in `tests/`.

Seats.aero specifics that aren't obvious from the code:

- Costs and availability use one-letter cabin prefixes: `Y` economy, `W` premium, `J` business, `F` first (`JMileageCost`, `JAvailable`, ...). `parse_availability` flattens one API row into one result per available cabin.
- Pagination is `skip` (rows already fetched, not results) plus the **first** page's `cursor`. Each page is one of the 1,000 daily calls, and `search()` raises rather than truncating at `max_pages`.
- Data is a cached snapshot. Check `updated_at`, since rows can be months stale. Live Search is not available on Pro.
- `remaining_seats` can be 0 while a cabin is flagged available. By the owner's decision, legs with 0 seats are treated as unbookable and dropped in `itineraries.search_one_way` (`MIN_SEATS`). Caveat: some programs report 0 on most results (Azul, Aeromexico, American ~78%), so this may over-exclude.
- Seats.aero only knows airport pairs that programs publish; it does not stitch itineraries. A pair with no route returns an empty result for every date (e.g. RNO->SJO), so search via gateway hubs (RNO->LAX, LAX->SJO); `itineraries.search_active_trips` does this using each trip's `trip_gateways`. Seats.aero returns dates but no flight times, so paired legs are not verified to connect. Use the `/routes?source=<id>` endpoint to check whether a pair exists.
- Program `source` ids: united, american, delta, alaska (Atmos), qantas, lifemiles, flyingblue, british (Avios). Southwest returns no routes.
- `db.active_trips()` is the single gate for the `active` rule below. Route every trip-driven job through it.
- Ranking prefers fewer self-transfers over lower miles (`Itinerary.self_transfers`): one ticket is a heuristic (same program and same-day connection), because separate tickets mean bags are re-checked and the airline doesn't cover a missed connection. It's the owner's stated preference, so don't rank by price alone.
- Affordability: each program's own balance is used first, then the shared Capital One balance covers shortfalls only for partners listed in `CAPITAL_ONE_TRANSFERS` (verified 1:1 only; an unlisted program must be paid from its own balance). Round trips are assessed jointly so both directions share balances.
- Cash fares: SerpApi's free plan is 250 searches a month, so always go through `cash_fares.get_fares` (cached in `cash_fare_cache`, 7-day freshness) and never call `SerpApiClient.search` directly in loops. `requests` exception text includes the full URL with the `api_key`, so the client re-raises without it. Never print or log raw `requests` errors from that client.
- The cash baseline is the cheapest through fare with layovers <= 4h and total <= 15h; the limits swing the result heavily. Savings figures exclude award taxes and fees.
- Tests use fake HTTP sessions. Only make live calls deliberately, since they spend quota.

## Architecture (draft, from README)

Four layers, none built yet:

- **Scheduler**: one daily run. Whether it's a cloud function (Lambda / Cloudflare Workers) or a local scheduled script is still undecided.
- **Data layer**: SQLite holding saved trips, program balances, and alert history. Alert history exists so the same fare isn't alerted twice.
- **Logic layer**: a rules engine comparing live results against the owner's thresholds (e.g. business class ≤ N miles, cash below $X).
- **Notification**: Pushover, ntfy.sh, or Twilio email/SMS. One is chosen for v1.

Data sources: award space comes from the Seats.aero API. Airline sites are deliberately not scraped (ToS, and they break often). Cash fares come from Skyscanner, Amadeus, or Kiwi.com, still to be chosen.

## Rules that span the whole design

- **The `active` flag gates everything.** Each trip has an `active` boolean, default off. The daily run and every alert, including buy-miles promo alerts, apply only to active trips. If no trip is active, nothing fires, however good a deal is. Setting a trip inactive must stop its alerts immediately. Any new alert type has to respect this.
- **Don't re-alert** on something already surfaced in the last N days unless price or availability improved. This needs the alert-history table.
- **Buy-miles promos are a different kind of data.** No API exists, since promos are announced via emails and blogs. The plan is RSS/scraping of deal sites (The Points Guy, Doctor of Credit, One Mile at a Time). It converts the bonus % to effective ¢/mile using manually hardcoded per-program baseline prices, and alerts only if the price is below threshold and the owner's balance is short for an active trip's business-class redemption.
- **Tracked programs:** Capital One, Delta SkyMiles, American AAdvantage, United MileagePlus, Qantas, Alaska/Hawaiian (Atmos), Southwest. Capital One miles transfer 1:1 to LifeMiles, Avios, Flying Blue, Qantas, and also Air Canada Aeroplan (the README's list omits it; Capital One has ~18 airline partners). Delta, American and United can't receive transfers, so their balances are tracked separately.
- **Southwest** has no alliance or transfer partners. It is tracked for completeness but should not factor into most award searches.
- Award pricing is increasingly dynamic, so thresholds are expected to need periodic re-tuning and shouldn't be hardcoded deep in logic.

## Secrets and local data

API keys live in `.env` (git-ignored). `.env.example` is the tracked template, so update it whenever a new variable is introduced. The SQLite database (`*.db`, `*.sqlite*`) is git-ignored because it holds personal balance and trip data.
