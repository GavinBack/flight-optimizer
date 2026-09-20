# TODO

Derived from `README.md`. Ordered roughly by dependency: decisions first, then accounts, then build.

## 0. Decisions to make first

These block design choices further down.

- [x] Check Seats.aero pricing and API limits: no free API. Pro ($9.99/mo, personal use) gives 1,000 calls/day. Subscribed; consider cancelling when no trip is active.
- [x] Pick a cash-fare source: **SerpApi Google Flights** (chosen). Research done (Sept 2026): none of the three named options is usable as an individual developer. Amadeus Self-Service is reported shut down since 2026-07-17 (from two third-party articles; Amadeus's own pages would not load, so verify). Skyscanner's official API is partner-only, and Kiwi.com Tequila is invite-only. Best fit found: SerpApi's Google Flights API (free plan: 250 searches/month, 50/hour; `travel_class` supports economy and business; IATA airport codes; one-way or round trip). Fallback: Sky Scrapper on RapidAPI (unofficial, ~100 requests/month). It must return both business and economy fares and cover small airports like RNO and SJO. Quota plan: 250 a month is about 8 a day, so refresh cash prices weekly or only for the top N options, not daily.
- [ ] Pick one notification channel for v1: Pushover, ntfy.sh, or email/SMS via Twilio.
- [ ] Pick where the scheduler runs: cloud function (Lambda / Cloudflare Workers) or a local scheduled script.
- [x] Pick a language and stack: Python 3.12 with a `.venv` virtual environment, SQLite for storage.
- [ ] Decide how "good" is measured, since this drives the rules engine:
  - [ ] Cash threshold per trip (the "$X" in the README). Costa Rica trip has none set; column exists (`max_cash_price`).
  - [x] Max miles for business class: none for now (Costa Rica trip is uncapped). Column exists (`max_miles`); NULL means no limit.
  - [ ] N days before re-alerting on the same result

## 1. Accounts to set up

Free, no balance needed. Status column mirrors the README table.

- [ ] Avianca LifeMiles
- [ ] British Airways Executive Club (Avios)
- [ ] Air France/KLM Flying Blue
- [ ] Qantas Frequent Flyer
- [ ] United MileagePlus
- [ ] Delta SkyMiles
- [ ] American AAdvantage
- [ ] Alaska/Hawaiian (Atmos Rewards)
- [ ] Southwest Rapid Rewards
- [x] Seats.aero account and API key
- [x] SerpApi account and API key (free plan). Confirmed 2026-09-19: returns business and economy prices for RNO>SJO (2 of 250 monthly searches used).
- [ ] Notification service account/token

## 2. v1: single route, single alert channel, manual balances

- [x] Project scaffold: repo layout, dependency management, config and secrets handling (API keys stay out of git)
- [x] `.gitignore` for secrets, `.env`, and the SQLite database file
- [x] SQLite schema:
  - [x] `trips` table: origin, destination, depart date, return date, `active` flag (default off)
  - [x] `balances` table: one row per program
  - [x] `alert_history` table: what was alerted, when, and at what price/availability, to prevent re-alerts
- [ ] Manual balance entry for all 7 tracked programs (Capital One, Delta, AAdvantage, MileagePlus, Qantas, Atmos, Southwest)
- [x] Seats.aero client: search award availability for one route and date range
- [x] Map README programs to Seats.aero `source` ids: united=MileagePlus, american=AAdvantage, delta=SkyMiles, alaska=Atmos, qantas=Qantas, lifemiles=LifeMiles, flyingblue=Flying Blue, british=Avios. `southwest` returned zero routes, so it is likely not covered.
- [x] Gateway searches: per-trip gateway airports (`trip_gateways`), search direct pair plus origin->hub and hub->destination, combine legs (`itineraries.py`). Costa Rica trip uses LAX, SFO, DEN, DFW.
- [ ] Layover feasibility: Seats.aero gives dates but no flight times, so a same-day pairing (e.g. RNO>LAX and LAX>SJO on Jan 9) may not actually connect. Decide whether to default to requiring the second leg the next day, or to just flag it for manual checking.
- [x] Balance-aware ranking (`affordability.py`): own balance first, then Capital One transfers for verified 1:1 partners, shared Capital One pool, round trips assessed jointly.
- [ ] Verify the remaining Capital One transfer ratios (Virgin Red -> Flying Club, JetBlue 5:3, others) and add them to `CAPITAL_ONE_TRANSFERS`. Also confirm transfers really move in 1,000-mile steps (assumed).
- [ ] Connecting legs (marked `*`) can be odd detours, e.g. RNO>LAX connecting on American before LAX>SJO. Consider penalizing or flagging them.
- [ ] Apply the trip's `max_miles` / `max_cash_price` limits in ranking once their meaning is settled (total vs per program).
- [x] Programs outside the README list: Air Canada Aeroplan IS a Capital One transfer partner (1:1, instant), so the README's four-partner list is incomplete. Still to confirm: Virgin Atlantic (Capital One lists Virgin Red, and how that reaches Flying Club is unverified) and JetBlue. Update the README table and add `aeroplan` to the tracked programs.
- [x] **0 seats = unbookable** (decided 2026-09-19; `itineraries.MIN_SEATS = 1`, adjustable per call via `min_seats`, e.g. 2 for two travelers). Effect on the Costa Rica report: combinations 4,148 -> 1,216, affordable 12 -> 8, best option 85,900 -> 89,900 miles. Risk: some programs report 0 on most results (Azul 100%, Aeromexico 93%, American 78%, United 15%; all others 0%), which may mean "count unknown". Revisit if American or United results look wrongly missing.
- [ ] Demote or exclude stale award legs (older than 7 days). The current #1 option still includes a Delta LAX>RNO leg last refreshed 2026-08-16.
- [x] **Prefer single-ticket itineraries** (`Itinerary.self_transfers`, ranking key `affordable, self_transfers, miles`). One ticket = same program and same-day connection (heuristic). Report shows a `tickets:` line per option and what the preference costs versus the cheapest option.
  - Costa Rica result (2026-09-20): no affordable option is a single ticket. Only 4 single-program itineraries exist per direction, all United or Alaska (68,500 to 165,000 miles each way); United balance is 3,765 and neither United nor Alaska takes Capital One transfers.
  - [ ] Among self-transfer options, consider preferring an overnight buffer at the hub over a same-day connection. Same-day is the riskier one on separate tickets, but overnight costs a day of the trip, so this is the owner's call.
  - [ ] Verify that a same-program two-leg itinerary really books as one ticket at that price. Programs may price the combined route differently from the sum of its legs.
  - [ ] Show the protected alternative: a cash through-ticket (already fetched as the baseline) is always a single ticket, so list it next to the award options when none is a single ticket.
- [ ] Verify the unit of `total_taxes` from Seats.aero before comparing it to cash fares
- [x] Cash-fare client (`cash_fares.py`): SerpApi Google Flights, one-way searches, cached in SQLite for 7 days so re-runs are free.
- [ ] SerpApi response shape (seen live, one-way RNO>SJO Jan 9, `type=2`, `travel_class` 1=economy 3=business): `best_flights` and `other_flights`, each with `price`, `flights[]` (per-segment airline, flight number, cabin label), `layovers[]` (minutes), `total_duration`; plus `price_insights` with `lowest_price`, `typical_price_range` and `price_history` (usable as a ready-made baseline). Through fares exist for RNO>SJO even though Seats.aero has no direct award route.
- [x] Baseline rule (`cheapest_reasonable`): cheapest fare with every layover <= 4h and total <= 15h (`DEFAULT_MAX_LAYOVER_MINUTES` / `DEFAULT_MAX_TOTAL_MINUTES`). These limits move the result a lot: the unfiltered cheapest business fare on RNO>SJO was $618, versus $1,505 with the limits. Revisit if they feel wrong.
- [ ] Cabin labels in cash results are per segment: a business search shows domestic legs as First Class and international legs as Business Class. Check whether Seats.aero's domestic `J` results are also first class, so comparisons are like for like.
- [ ] Round trips: search two one-ways (`type=2`) per direction, which matches how the award legs are priced. Round-trip pricing (`type=1`) is a different product, so only use it as a cross-check.
- [ ] **Cash price for every leg where points are used** (per-leg, not through fare). Through-fare baselines are done; per-leg is still open.
- [x] **Economy cash baseline**, whole-trip through fares per direction (`savings.py`). Still open: economy fares for the individual award legs (see the per-leg item above).
- [x] **Savings and value per mile** (`savings.estimate_value`): cents per mile against both the award-cabin and economy baselines, plus the upgrade premium. Before award taxes and fees, since the `total_taxes` unit is still unverified, so these are upper bounds.
- [x] Store cash-price snapshots with a fetched-at time (`cash_fare_cache` table). Keeps every fare, so baseline limits can change without re-searching.
- [ ] Limit cash lookups to the top N ranked options. Not needed for through-fare baselines (about 4 searches per trip), but required if per-leg cash prices are added.
- [x] Report (`python -m flight_optimizer [--top N]`, `report.py`): per active trip, cash baselines, top affordable options with cents per mile, how to pay (own balances vs Capital One transfers), named warnings, and closest misses.
- [ ] Cache Seats.aero searches briefly (e.g. 1 hour). Each report run costs about 18 of the 1,000 daily calls, and repeat runs re-spend them.
- [ ] Award data freshness: several legs in the top options were last refreshed weeks ago (Jul 29, Aug 16). Consider showing freshness on every leg, not just warnings, and preferring fresher data.
- [ ] Scheduler that runs once daily and only processes trips with `active = true`
- [ ] Rules engine:
  - [ ] Alert if a business-class award is bookable within current balance
  - [ ] Alert if cash fare is below the threshold
  - [ ] Alert if award space opens on a previously sold-out route/date
  - [ ] Suppress repeat alerts within N days unless price/availability improves
- [ ] Notification sender for the chosen channel
- [ ] Confirm nothing fires for an inactive trip. Marking a trip inactive must stop alerts immediately.
- [ ] End-to-end test with one real route

## 3. Buy-miles promo alerts

Separate alert type. It depends on at least one active trip.

- [ ] Choose sources: RSS or scraping for The Points Guy, Doctor of Credit, One Mile at a Time
- [ ] Filter posts for "buy miles" plus a program name
- [ ] Hardcode a "normal price" baseline (¢/mile) per program, since no structured feed exists (README open question)
- [ ] Convert bonus % into effective ¢/mile
- [ ] Alert only when all three hold:
  - [ ] At least one trip is `active`
  - [ ] Effective ¢/mile is below the threshold for that program
  - [ ] Current balance is short of what the active trip's business-class redemption needs
- [ ] Set per-program ¢/mile thresholds (e.g. LifeMiles ≤ ~1.5¢ against a normal ~2.5–3¢)

## 4. v2: multiple trips and price history

- [ ] Support multiple saved trips at once
- [ ] Store historical price and availability snapshots
- [ ] Use history to tune thresholds. The README notes dynamic award pricing means thresholds need periodic re-tuning.

## 5. v3: web UI

- [ ] Replace config file/script with a simple web UI
- [ ] UI to add/edit trips and toggle `active`
- [ ] UI to update balances

## 6. v4 (maybe): transfer suggestions

- [ ] Suggest which program to transfer Capital One miles to, based on live award pricing across reachable partners (LifeMiles, Avios, Flying Blue, Qantas)

## 7. Optional: Claude advisor, two-trip planning, and a local GUI

Nice-to-haves once the core alerting works. Item 7.3 overlaps with section 5 (v3 web UI), so build them as one.

- [ ] **Claude API advisor.** Add the Claude API so the app can reason over the ranked options, recommend actions, and lay out the pros and cons of each option (e.g. "transfer 50,000 to Aeroplan now, or wait for a cheaper Alaska leg").
  - [ ] Keep the arithmetic (affordability, transfers, savings, cents per mile) in code. Send Claude the computed results and use it for judgment and explanation only.
  - [ ] Send only the top N options plus balances, cash prices and savings, to keep each call small.
  - [ ] Add `ANTHROPIC_API_KEY` to `.env` and `.env.example`. This is pay-per-use API billing (no subscription), so estimate the monthly cost first.
  - [ ] Have it flag risks the data can't settle, such as unverified connection times, cached data age, and irreversible Capital One transfers.
- [ ] **Two trips at a time.** Let two active trips be entered and planned together, finding ways to optimize total cost across both.
  - [ ] Share balances across trips: the same Capital One pool and per-program balances must cover both. `affordability.assess` already takes several itineraries, so extend it to pairs of trip options.
  - [ ] Choose the objective: fewest total miles, most total savings, or least cash spent.
  - [ ] Prune to the top K options per trip before combining, since 4,000+ options per trip multiplies out quickly.
  - [ ] Surface conflicts and trade-offs, e.g. spending Aeroplan miles on trip A leaves trip B short.
- [ ] **Locally hosted GUI** for all of the above: trips and the `active` toggle, balances, ranked options with cash price, savings and cents per mile, and Claude's recommendation. Pick a framework (e.g. Streamlit, Flask or FastAPI). Bind to `127.0.0.1` only, since it shows balances and sits next to API keys.

## Notes

- Southwest has no alliance or transfer partners, so it's tracked for completeness but won't factor into most award searches.
- Delta, American, and United can't receive Capital One transfers. Their balances are tracked and earned separately.
- No auto-booking is planned. This is a research and alerting tool only.
