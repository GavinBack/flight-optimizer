# TODO

Derived from `README.md`. Ordered roughly by dependency: decisions first, then accounts, then build.

## Where things stand (cleaned up 2026-09-21)

The core system is built and in daily use (see `CLAUDE.md` for what exists). What is actually open:

- **Yours to do:** confirm the balances that read "never set" (Atmos, Flying Blue, LifeMiles, Avios, JetBlue) are really zero. Your Aeroplan account is open (balance 0, 2026-09-21). Virgin Red and Flying Club are deferred until a Virgin option is worth pursuing (section 1).
- **First unattended run:** watch `logs/notify.log` after the next 7:00 AM run, the first with advice, failure alerts, flexible Kelowna, cash history and the shared-balances check all live.
- **Trip dates are departure dates by default (decided 2026-10-03, replacing 2026-09-22):** `--depart` dates are the days to leave. `--arrive` is an explicit alternative: the day to land, and the outbound may leave up to 1 day earlier; `--flexible` is 3 days either side of each date for both kinds (for `--arrive`, of the day the flight there lands). Costa Rica and Kelowna keep arrival dates (Kelowna lands Dec 19 to 25). The `trips.depart_date` column is now `outbound_date`. Open follow-up: cash fares still price flights leaving on the landing day and store no arrival times, so a red-eye landing the next day can set the cash baseline. Storing SerpApi arrival times would fix it.
- **Known limits, not blockers:** award data is often stale (Seats.aero's cache); buy-miles sale alerts rarely fire because Delta, United and American sales are headline-only; award prices and availability are not stored (only cash history); trips past their dates stay active; the database has no off-machine backup; two trips use about 180 of the 250 free SerpApi searches a month.
- **Optional, later:** the local GUI and web UI, transfer suggestions, the earn/status model (on hold until you pick a program), buy-versus-transfer comparison.

Everything below is the detailed record; items marked done are done.

## 0. Decisions to make first

These block design choices further down.

- [x] Check Seats.aero pricing and API limits: no free API. Pro ($9.99/mo, personal use) gives 1,000 calls/day. Subscribed; consider cancelling when no trip is active.
- [x] Pick a cash-fare source: **SerpApi Google Flights** (chosen). Research done (Sept 2026): none of the three named options is usable as an individual developer. Amadeus Self-Service is reported shut down since 2026-07-17 (from two third-party articles; Amadeus's own pages would not load, so verify). Skyscanner's official API is partner-only, and Kiwi.com Tequila is invite-only. Best fit found: SerpApi's Google Flights API (free plan: 250 searches/month, 50/hour; `travel_class` supports economy and business; IATA airport codes; one-way or round trip). Fallback: Sky Scrapper on RapidAPI (unofficial, ~100 requests/month). It must return both business and economy fares and cover small airports like RNO and SJO. Quota plan: 250 a month is about 8 a day, so refresh cash prices weekly or only for the top N options, not daily.
- [x] Notification channel: **ntfy** (chosen 2026-09-20). Alert = short summary plus an urgency line; the step-by-step plan is read in the app on the laptop (`plan`), not on the phone.
- [x] Pick where the scheduler runs: a local launchd job on the owner's Mac (decided; see section 2).
- [x] Pick a language and stack: Python 3.12 with a `.venv` virtual environment, SQLite for storage.
- [x] Decide how "good" is measured, since this drives the rules engine:
  - [x] Cash threshold per trip and cabin (the "$X" in the README): `trip target TRIP CABIN AMOUNT` (2026-09-21). The old `trips.max_cash_price` column was dropped.
  - [x] Max miles: none. Trips are uncapped, and the `max_miles` column was dropped (2026-09-21). Whether an award is worth its miles is judged by cents per mile after taxes against the 1 cent floor.
  - [x] N days before re-alerting on the same result: 7 (`RE_ALERT_DAYS`), unless it got cheaper.

## 1. Accounts to set up

Free, no balance needed. Status column mirrors the README table.

Done: Aeroplan (2026-09-21, balance 0; it is where every affordable award so far runs, and the Capital One transfer to it is now possible). Deferred by the owner: Virgin Red and Flying Club, to be opened if and when a Virgin option is worth pursuing. Open the others only if you expect to use them. None is needed for the app to run.

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
- [x] ntfy topic: generated into `.env` as `NTFY_TOPIC` (long and random; it acts as a password on the public server). Subscribe to it in the ntfy app, then run `python -m flight_optimizer test-notification`.

## 2. v1: single route, single alert channel, manual balances

- [x] Project scaffold: repo layout, dependency management, config and secrets handling (API keys stay out of git)
- [x] `.gitignore` for secrets, `.env`, and the SQLite database file
- [x] SQLite schema:
  - [x] `trips` table: origin, destination, depart date, return date, `active` flag (default off)
  - [x] `balances` table: one row per program
  - [x] `alert_history` table: what was alerted, when, and at what price/availability, to prevent re-alerts
- [x] Manual balance entry: `python -m flight_optimizer balance PROGRAM MILES ...` (all programs). Capital One, Southwest, American, Delta and United are set; Atmos, Flying Blue, LifeMiles, Avios and JetBlue read 0 and show "never set": the owner should confirm those are truly zero.
- [x] Seats.aero client: search award availability for one route and date range
- [x] Map README programs to Seats.aero `source` ids: united=MileagePlus, american=AAdvantage, delta=SkyMiles, alaska=Atmos, qantas=Qantas, lifemiles=LifeMiles, flyingblue=Flying Blue, british=Avios. `southwest` returned zero routes, so it is likely not covered.
- [x] Gateway searches: per-trip gateway airports (`trip_gateways`), search direct pair plus origin->hub and hub->destination, combine legs (`itineraries.py`). Costa Rica trip uses LAX, SFO, DEN, DFW.
- [x] Layover feasibility (`connections.py`, `enrichment.py`): flight times come from Seats.aero's per-result `trips` lookup (one call each, local times). Rules: same-day with 2.5h+ is preferred, over 1h and under 2.5h warns, 1h or less is never recommended, an overnight stop ranks below any same-day connection. Also applied to cash fares (a layover of 1h or less is excluded, under 2.5h warns).
- [x] Balance-aware ranking (`affordability.py`): own balance first, then Capital One transfers for verified 1:1 partners, shared Capital One pool, round trips assessed jointly.
- [x] Capital One transfer ratios verified 2026-09-20 (Capital One's partner page and upgradedpoints agree): Aeroplan, LifeMiles, Flying Blue, Avios, Qantas, Virgin Red all 1:1; JetBlue 5:3. Virgin Atlantic and JetBlue are now in `CAPITAL_ONE_TRANSFERS`. Transfer times are not instant everywhere: almost instant (Aeroplan, LifeMiles, Flying Blue, Avios), Qantas up to 36 hours, JetBlue 2 to 5 days, Virgin about 1 business day (confirmed by the owner on Capital One's screen: the transfer goes to a Virgin Red membership number; to book Virgin Atlantic flights the plan says to also open a free Flying Club account and link it to Virgin Red). The plan and alerts now state these and warn about slow ones. Not fully confirmed: Virgin's own support pages could not be read (403), so the Virgin Red to Flying Club link rests on Capital One's listing plus secondary sources; the 1,000-mile transfer step is still an assumption (Capital One states only the minimum; one source says 100-mile steps for Virgin), which can only over-count.
- [x] Connecting legs (marked `*`): the report now shows the stop count per leg, and multi-stop hub legs are penalized in the ranking (see the next items).
- [x] Trip `max_miles` / `max_cash_price` limits: dropped as unused (2026-09-21). Cash targets (`trip target`) replaced the price limit; there is no miles cap.
- [x] Programs outside the README list: Air Canada Aeroplan IS a Capital One transfer partner (1:1, instant), so the README's four-partner list is incomplete. Still to confirm: Virgin Atlantic (Capital One lists Virgin Red, and how that reaches Flying Club is unverified) and JetBlue. Update the README table and add `aeroplan` to the tracked programs.
- [x] **0 seats = unbookable** (decided 2026-09-19; `itineraries.MIN_SEATS = 1`, adjustable per call via `min_seats`, e.g. 2 for two travelers). Effect on the Costa Rica report: combinations 4,148 -> 1,216, affordable 12 -> 8, best option 85,900 -> 89,900 miles. Risk: some programs report 0 on most results (Azul 100%, Aeromexico 93%, American 78%, United 15%; all others 0%), which may mean "count unknown". Revisit if American or United results look wrongly missing.
- [x] Stale award data (older than 7 days, `itineraries.STALE_AWARD_DAYS`) is demoted in the ranking and named in the report and alert. Decision (2026-09-20): demote, not exclude.
- [x] Hub legs with several flights: extra stops are penalized in the ranking, shown per leg in the report, and called out in the alert at 2+ stops. Decision (2026-09-20): penalize, not require nonstop. Ranking is now (self-transfers, connection class, hub-leg stops, stale legs, miles). Measured on live data, a hard filter would have left nothing: of 9 affordable options, 0 have nonstop hub legs, only 2 have all data under 7 days old, and 0 satisfy both.
  - [x] Staleness severity: resolved by the freshness rule below (2026-09-20). The stale Virgin-based options (data 20 and 31 days old) now rank behind the fresher 104,000-mile options.
  - [ ] Virgin Atlantic options: Virgin Red IS on the owner's Capital One transfer screen (asks for a Membership #). Deferred by the owner (2026-09-21, not urgent): open Virgin Red and Flying Club accounts (free), link them, and verify the exact flights on Virgin's site before any transfer, if and when a Virgin option is worth pursuing.
- [ ] Booking links from Seats.aero pre-fill route and date but not always the cabin (the Qantas link opened `travelClass=ECO`). The plan warns about it; consider verifying cabin handling per program.
- [ ] Enrichment only checks the leading 12 candidates per direction (max 20 lookups each), ordered by price before connections are known. For the Costa Rica trip an exhaustive check gave the identical result, but a trip with many comfortable options further down could be missed.
- [ ] Urgency thresholds (2 seats = act today, up to 5 = a few days) are guesses. Once alert history builds up, measure how long options actually last and tune them.
- [x] Daily schedule installed (7:00 AM).
- [x] **Prefer single-ticket itineraries** (`Itinerary.self_transfers`, ranking key `affordable, self_transfers, miles`). One ticket = same program and same-day connection (heuristic). Report shows a `tickets:` line per option and what the preference costs versus the cheapest option.
  - Costa Rica result (2026-09-20): no affordable option is a single ticket. Only 4 single-program itineraries exist per direction, all United or Alaska (68,500 to 165,000 miles each way); United balance is 3,765 and neither United nor Alaska takes Capital One transfers.
  - [x] Overnight buffer decision (2026-09-20): prefer same-day connections with a 2.5h buffer; warn between 1h and 2.5h; never recommend 1h or less. Ranking is now (self-transfers, connection class, miles).
  - [ ] Verify that a same-program two-leg itinerary really books as one ticket at that price. Programs may price the combined route differently from the sum of its legs.
  - [x] The cash through-ticket is listed next to the awards in the report and plan whenever a fare exists, with its own layover warnings and a comparison against award #1.
- [x] Unit of `total_taxes` confirmed for CAD (2026-09-21): Aeroplan LAX>SJO 10600 = CAD 106.00 on aircanada.com, so hundredths. Seats.aero's own docs give no unit. Still unchecked: USD (very likely: 560 = $5.60, the US September 11 fee), AUD (Qantas, 3 stored flights at 1410), and any zero-decimal currency (none seen). A second Aeroplan amount (SJO>LAX 16910 = CAD 169.10) would pin the scale further.
- [x] Taxes are folded into cents per mile, the cabin comparison and the value floor (2026-09-21, `taxes.py`): converted at fixed approximate rates (CAD 0.72, AUD 0.65, EUR 1.08, GBP 1.27), shown as `+ $305 taxes`. Live effect: Costa Rica's economy award, 49,000 miles + $208 taxes against a $699 fare, sits right at the 1 cent floor (about $698 against $699).
  - [x] The exchange rates (`taxes.USD_PER_UNIT`), the assumption that USD and AUD are hundredths (only CAD is confirmed), and MXN and BRL having no rate are all ACCEPTED by the owner (2026-09-21): the tool is a guide, and the owner confirms every figure on the program's own site before buying. Update the rates by hand if they drift; do not treat the remaining checks as blockers.
- [x] Cash-fare client (`cash_fares.py`): SerpApi Google Flights, one-way searches, cached in SQLite for 7 days so re-runs are free.
- [ ] SerpApi response shape (seen live, one-way RNO>SJO Jan 9, `type=2`, `travel_class` 1=economy 3=business): `best_flights` and `other_flights`, each with `price`, `flights[]` (per-segment airline, flight number, cabin label), `layovers[]` (minutes), `total_duration`; plus `price_insights` with `lowest_price`, `typical_price_range` and `price_history` (usable as a ready-made baseline). Through fares exist for RNO>SJO even though Seats.aero has no direct award route.
- [x] Baseline rule (`cheapest_reasonable`): cheapest fare with every layover <= 4h and total <= 15h (`DEFAULT_MAX_LAYOVER_MINUTES` / `DEFAULT_MAX_TOTAL_MINUTES`). These limits move the result a lot: the unfiltered cheapest business fare on RNO>SJO was $618, versus $1,505 with the limits. Revisit if they feel wrong.
- [ ] Cabin labels in cash results are per segment: a business search shows domestic legs as First Class and international legs as Business Class. Check whether Seats.aero's domestic `J` results are also first class, so comparisons are like for like.
- [ ] Round trips: search two one-ways (`type=2`) per direction, which matches how the award legs are priced. Round-trip pricing (`type=1`) is a different product, so only use it as a cross-check.
- [ ] **Cash price for every leg where points are used** (per-leg, not through fare). Through-fare baselines are done; per-leg is still open.
- [x] **Economy cash baseline**, whole-trip through fares per direction (`savings.py`). Still open: economy fares for the individual award legs (see the per-leg item above).
- [x] **Savings and value per mile** (`savings.estimate_value`): cents per mile against both the award-cabin and economy baselines, plus the upgrade premium. Now after award taxes and fees (2026-09-21). Before that they excluded taxes, because the `total_taxes` unit was unverified, so they were upper bounds on value.
- [x] Store cash-price snapshots with a fetched-at time (`cash_fare_cache` table). Keeps every fare, so baseline limits can change without re-searching.
- [ ] Limit cash lookups to the top N ranked options. Not needed for through-fare baselines (about 4 searches per trip), but required if per-leg cash prices are added.
- [x] Report (`python -m flight_optimizer [--top N]`, `report.py`): per active trip, cash baselines, top affordable options with cents per mile, how to pay (own balances vs Capital One transfers), named warnings, and closest misses.
- [x] Cache Seats.aero calls (`seats_cache.py`, `CachedSeatsAero`). Flight lookups are cached with no time limit, keyed by (row id, the row's `UpdatedAt`): verified on live data that the row's stamp equals every flight entry's stamp, so an unchanged stamp means unchanged flights. Searches have no version stamp, so they expire after 1 hour (helps repeat runs, never the daily job). `--fresh` skips the search cache; `plan --refresh` implies it. Measured on the Costa Rica trip: cold run 43 API calls (17 searches + 26 flight lookups); repeat run 0; next-morning run 17 (searches only, all 26 lookups reused). Real mornings will re-look-up whichever rows Seats.aero refreshed overnight, so 17 is the floor.
  - [x] Gateways pruned (2026-09-20): Costa Rica now uses LAX only. Measured on live data, DFW returned no itineraries, and DEN and SFO returned some but none fundable with current balances, none in an affordable option, none among the 10 closest misses. Results were identical before and after. A fresh run now costs 7 API calls (6 searches + 1 new flight lookup, rest cached), down from 43 cold / 17 warm. Caveat: SFO and DEN were unfundable *with current balances*; if balances change materially (more Delta or United miles), re-check them. Gateways are stored in the local database, not in git.
  - [x] CLI to view and change gateways and balances (`gateways`, `balance`): friendly program names and aliases, unique prefixes, commas in amounts, all-or-nothing updates, stale-balance flag (30+ days), and a per-run search-cost estimate for gateway changes.
  - [x] Balances no longer go stale silently: the weekly digest names any non-zero balance not updated in 30+ days and says how to update it (never the amount).
- [x] **Data freshness rule** (owner: "offers and availability should be current"). Award data is tiered by age: current (3 days or newer), aging (4 to 7), stale (over 7). An option is judged by its stalest leg, and freshness is the top ranking criterion after affordability: (affordable, freshness, self-transfers, connection class, hub-leg stops, stale legs, miles). Alerts never fire on options resting on stale data, and `notify` says so when affordable options exist but all are stale. Every option in the report shows a data label. Cash offers: fares are shown with their fetch date and refreshed once older than 2 days (was 7), about 60 of the 250 monthly SerpApi searches for one round-trip trip (a second active trip would double that).
  - [x] The borderline happened (2026-09-21): the Qantas leg turned 8 days old overnight, every affordable option became stale, and the first scheduled run correctly stayed silent (`logs/notify.log`: "every one relies on award data older than 7 days"). Answer: the **weekly digest** (`digest.py`), sent by the daily run once every 7 days (the first on the next run) and on demand with `python -m flight_optimizer digest [--dry-run]`. It states, per trip, how many options exist and how many are current, aging or stale, that silence means no current data and not no options, the alerts sent in the last 7 days, the cheapest gap to close by buying (a headline-only sale is called a best case), the live sales count, and whether balances are stale. Low priority, never includes balance amounts, and the weekly timer only restarts once a digest was really sent.
  - [ ] The freshness thresholds (3 and 7 days) are judgment calls. Once alert history builds up, check how often Seats.aero refreshes routes like RNO>LAX and adjust.
- [x] Scheduler: a launchd job (`com.gavinback.flightoptimizer.notify`, plist in `~/Library/LaunchAgents/`) runs `python -m flight_optimizer notify` daily at 7:00 AM local. Output goes to `logs/notify.log`. Verified under launchd before install: settings and database readable from `~/Documents`, network reachable, and a full `notify --dry-run` exits 0. The first real run will alert on the current best option. If the Mac is asleep at 7:00 it should run on wake (launchd behavior); if it is powered off the run is skipped.
- [x] Rules engine:
  - [x] Alert if a business-class award is bookable within current balance (`alerts.decide_alert`: alerts on the best affordable option)
  - [x] Alert if cash fare is below the threshold (`cash_alerts.py`: your `trip target`, plus an automatic economy drop alert once there is history)
  - [ ] Alert if award space opens on a previously sold-out route/date. In practice a newly affordable option already alerts once (`decide_alert`), so this only adds value with stored award history (not built).
  - [x] Suppress repeat alerts within N days unless price improves (`RE_ALERT_DAYS = 7`, keyed on the exact flights)
- [x] Notification sender (`notify.py`, ntfy over JSON; errors never leak the topic).
- [x] Confirm nothing fires for an inactive trip (tested: only `db.active_trips` are searched or alerted on; `done <trip>` stops alerts immediately).
- [x] End-to-end test with one real route: done repeatedly on live data (reports, dry-run notifies, a live advice call, a failure path with a wrong key), plus the daily launchd job running unattended.

## 3. Buy-miles promo alerts

Separate alert type. It depends on at least one active trip.

- [x] Choose sources (researched 2026-09-20, all read-only): **Live From A Lounge** is the best fit. Its RSS feed carries structured titles such as "Deal: Buy Aeroplan miles with up to 100% Bonus through September 25, 2026" (program, bonus and end date in the title), and a weekly roundup page ("15 great miles and points buying opportunities available as of ...") lists every current offer with its purchase tiers and end date. Secondary: Doctor of Credit's main feed (titles like "Aeroplan: Buy Miles & Get Up To 115% Bonus"), but its buy-miles tag feed was abandoned in 2022.
  - Rejected: One Mile at a Time (search feed 404), The Points Guy (search returns HTML, not a feed), Frequent Miler (aggregate "Current Deals" posts, noisy prose). WordPress search feeds work on Doctor of Credit and Frequent Miler but return mostly unrelated posts.
  - Every feed only holds the last 3 to 5 days (10 to 25 posts), so poll daily and store the posts already seen. Be polite: one feed fetch a day, article pages only when needed, an identifying User-Agent.
  - Promos are prose, not data, and the headline is misleading for small purchases. Bonuses are tiered by purchase size (Aeroplan: 5k-25k gets 50%, 30k-75k gets 75%, 80k+ gets 100%; LifeMiles: 1k-20k 125%, 21k-50k 135%, 51k-200k 145%), and some offers are targeted ("varies per customer"). A 3,000-mile top-up gets the lowest tier, so "up to 145%" is the wrong number to use.
  - Live on 2026-09-20: Delta up to 50% bonus through Sept 30 (2.51 cents per mile at the top tier vs 3.76 retail including the 7.5% excise tax; minimum 2,000 miles, in steps of 2,000), Aeroplan up to 100% and LifeMiles up to 145% through Sept 25, JetBlue up to 120%, United up to 100%.
  - Retail (no-promo) prices seen: Delta and United 3.76 cents per mile including tax; American 2.25 to 3.76; Aeroplan and LifeMiles imply base prices near 2.7 and 3.3 cents (1.35 cents at the top tier divided by the bonus). These need verifying before they become baselines.
- [x] **Buy-to-close-gap analysis**: the report's "GAPS YOU COULD CLOSE BY BUYING MILES" section, ranked current data first then cheapest, each with the cash ticket price beside it and any sale applied. On 2026-09-20 buying is a poor deal for every current-data option (the best is $541.80 in a best-case American discount on top of 100,000 miles, against a $2,267 cash ticket). The $150.50 Delta gap sits behind stale data.
- [ ] **Delta's live sale is not in any feed I can read, or on Delta's public pages** (the Buy Miles page shows only the standard price; the sale terms appear in the logged-in purchase flow, and the cash fare deals page is unrelated) (announced Sept 13, before the feeds' 3-day window; LoyaltyLobby's article and search are blocked). Record it by hand with the real tiers from Delta's page: `python -m flight_optimizer promos add delta --bonus 50 --ends 2026-09-30 --tier 2000-9000:PERCENT ...`. Until then Delta gaps are priced at retail.
- [ ] Most programs you could be short in (Delta, United, American) only have a headline in the roundup, so their sales are best cases and never alert. Read tiers from each program's own sale page, or from the individual deal posts the roundup links to (not done: fetching those pages).
- [ ] Sales can be targeted ("varies per customer"): the terms you are offered may differ from the article. Always confirm on the program's page while logged in.
- [ ] Gaps in the Capital One pool are not priced: an option short in Capital One miles could instead buy points directly in the transfer program (Aeroplan, LifeMiles, Virgin) during a sale. `plan_purchases` returns None for those today.
- [ ] Compare buying against transferring: during a sale, buying Aeroplan points directly (about 1.35 to 1.8 cents) versus spending Capital One miles (worth at least 1 cent each as travel credit) is a real trade-off the report does not yet weigh.
- [ ] Purchase block sizes and annual limits for the other programs come from single secondary sources: verify on each program's buy page. Delta was checked on delta.com on 2026-09-21: price ($0.035 a mile plus tax) and the 60,000-a-year limit are confirmed (confidence "high"). Also learned there: miles post in 24 to 48 hours, purchases are final, and the account must be 10+ days old with a posted mile. Still unconfirmed: Delta's block size (delta.com does not state it) and its current sale, which only shows inside the logged-in purchase flow.
- [x] Filter posts (`promos.py`): a title must have a buy verb and a miles/points word and name a tracked airline; hotel programs, card offers (American Express is not American Airlines), expired posts and untracked programs are ignored. Sources: Live From A Lounge (feed titles plus the weekly roundup page, which carries the tier tables), Doctor of Credit and LoyaltyLobby (titles). A refresh is limited to once every 6 hours, reads each roundup page once, and one broken source does not stop the others.
- [x] Retail price table (`buying.RETAIL_PRICES`), verified 2026-09-20 against several secondary sources: US carriers 3.5 cents + 7.5% excise tax (Delta, United, American, Atmos; JetBlue 3.0 + tax), Aeroplan about 2.7 USD cents (3.75 CAD, moves with FX), LifeMiles 3.3, Avios 3.25 (tiered), Flying Blue 3.05 (euros), Virgin 2.5. Arithmetic cross-checks held (Delta's promo total is exactly 60,000 x 3.5c x 1.075). Confidence is medium for the US carriers and LifeMiles, low for the FX-priced and tiered programs. Qantas and Southwest are deliberately unpriced. All are ESTIMATES: the airlines' own pages were not read.
- [x] Effective cents per point (`buying.quote_purchase`): reads the tier for the size of the *purchase* (a small top-up gets the lowest tier), rounds up to the program's block size, respects annual limits, picks the smallest purchase that covers the gap, and compares against retail. A headline-only sale is priced as a labelled BEST CASE.
- [x] Sale alerts (`alerts.decide_promo_alert`) fire only when all hold: an active trip; a live sale with KNOWN tiers whose price per point is under the program's threshold on every purchase in the plan; and a balance that is short for that trip's option. Options resting on stale data never qualify. A repeat is suppressed for 7 days unless cheaper. Verified on live data: no alert today, because the only sales with known tiers (Aeroplan, LifeMiles, JetBlue) are for programs you are not short in.
  - [x] At least one trip is `active`
  - [x] Effective ¢/mile is below the threshold for that program
  - [x] Current balance is short of what the active trip's business-class redemption needs
- [x] Thresholds: 70% of the retail price per point (`PROMO_ALERT_FRACTION`), with LifeMiles overridden to 1.5 cents (the README's example). Both are constants in `buying.py` to tune.

## 4. v2: multiple trips and price history

- [x] **Failure alerts** (2026-09-21, `failures.py`): a broken run now sends a `Flight check ...` ntfy message instead of only writing to the log, one trip's failure no longer costs the others their alerts, and a run that starts over 36 hours after the last says the check was missed. Not covered: a Mac that stays off cannot report itself (the next run does), and a launchd job that never loads.

- [x] Multiple saved trips (2026-09-21): `python -m flight_optimizer trip [list|add|set]` adds, lists and changes trips (validated: airport codes, dates, gateways, and duplicate routes are refused). The report, daily check and digest already loop over every active trip. Commands that need one trip ask which (`Say which trip: 1 (Costa Rica), 2 (Kelowna)`). Trip 2 added: Kelowna, RNO > YLW, Dec 22 to Dec 28 2026, gateways SEA, LAX, PHX.
- [x] **Every trip is evaluated in economy, premium economy and business** (2026-09-21, owner's rule; the per-trip `--cabin` option is gone). Goals: 1. the best price and options for each trip; 2. notice when a better cabin becomes viable on cost. Built: one shared Seats.aero search per trip (no extra calls), per-cabin analysis, a "cabins at a glance" report section, per-cabin alerts and plans, a better-cabin alert for close cash fares, and a digest line. See CLAUDE.md for the design.
  - [x] Mile value (`MILE_VALUE_CENTS`, 1.0) and upgrade multiple (`UPGRADE_MAX_MULTIPLE`, 1.5) accepted by the owner, 2026-09-21.
  - [ ] **Earn and "gaming" model, on hold.** The owner's insight: a business fare earns miles faster, and miles can be traded, so business can cost less than its listing. The owner is not chasing a specific program now and will research that separately (the Capital One card is for flexibility, e.g. an urgent UK trip), so no per-program earn-rate research yet. When a program is chosen: subtract the value of the miles and status a cabin earns from its cost, using only legitimate earn and redemption effects.
  - [ ] Capital One is a flexibility reserve (e.g. an urgent UK trip). When joint evaluation is built, consider showing how much of the pool an option leaves untouched.
  - [ ] Premium economy is thin: no premium economy award space was found through LAX for either trip, and Google returned no premium economy cash fares for RNO to SJO. Worth checking whether that changes closer to the dates.
  - [ ] SerpApi budget: three cabins take a round-trip trip to about 90 searches a month, so two trips use about 180 of the 250 free. A third trip would exceed it: lengthen the cache age or drop premium economy cash searches for it.
- [ ] Store historical price and availability snapshots. Started for cash: `cash_history` keeps each trip and cabin's round-trip cash total per fresh search (2026-09-21). Award availability and prices are still not stored, which also blocks an "award space opened" alert.
- [ ] Cash history is used for the economy drop alert (10% and $50 under the median, at least 3 earlier searches). Use history to tune thresholds. The README notes dynamic award pricing means thresholds need periodic re-tuning.

## 5. v3: web UI

- [ ] Replace config file/script with a simple web UI
- [ ] UI to add/edit trips and toggle `active`
- [ ] UI to update balances

## 6. v4 (maybe): transfer suggestions

- [ ] Suggest which program to transfer Capital One miles to, based on live award pricing across reachable partners (LifeMiles, Avios, Flying Blue, Qantas)

## 7. Optional: Claude advisor, two-trip planning, and a local GUI

Nice-to-haves once the core alerting works. Item 7.3 overlaps with section 5 (v3 web UI), so build them as one.

- [x] **Claude API advisor** (2026-09-21): `python -m flight_optimizer advise [TRIP] [--dry-run]` (`advisor.py`). On demand only, so it costs nothing unless you run it.
  - [x] Arithmetic stays in code. Claude gets the computed report (top 3 options per cabin, cash fares, savings, the cabin comparison, and your balances) and gives judgment and explanation only. When two trips are shown it is told the tool has not checked them against each other, and to say so.
  - [x] Small calls: about 12,600 input tokens for both trips (measured on the first live call; `--dry-run` shows the estimate) and 4,150 output tokens that call used, counting thinking, with a cap of 8,000 (the model thinks before it answers, and thinking counts toward that limit; the answer itself is about 450 words). At Sonnet-class prices (roughly $3 per million tokens in and $15 out; not confirmed for `claude-sonnet-5`, so check the console) that is about 10 cents a call (12.6k in, 4.2k out), or about $3 a month at one call a day.
  - [x] `ANTHROPIC_API_KEY` (and optional `ANTHROPIC_MODEL`) added to `.env.example`. Pay-per-use billing: set a spend limit in the Anthropic console.
  - [x] It is asked to flag risks the data cannot settle (data age, unverified connections, self-transfers, slow or irreversible transfers, taxes and fees) and what would change its advice.
  - [x] Advice runs after every `notify` run (owner's choice, 2026-09-21), reusing that run's analyses; saved to `advice/` and shown with `advise --last`. Kept off the phone (it discusses balances). The launchd job needs `ANTHROPIC_API_KEY` in `.env`. About $3 a month at the daily job.
  - [x] The weekly digest ends with a one-line pointer to the latest advice (never its content). Advice is skipped when nothing has changed since the last answer (`advisor.fingerprint`: trips, balances, sales, and per cabin the cash fares in $50 steps and the leading options' miles, affordability and freshness tier). The daily cost is now about 10 cents only on days something moved.
  - [ ] The fingerprint has no maximum age: if nothing changes for weeks the old answer stands. Add a refresh after N days if that matters.
  - [ ] Not done: advice is not saved or compared between runs.
- [x] **Two trips at a time** (2026-09-21): each trip is analyzed alone, then `joint.py` checks them against each other on the shared balances. Findings so far: only Costa Rica has an affordable award, so there is no competition today.
  - Kelowna finding (2026-09-21): no direct RNO-YLW award route exists; SEA, LAX and PHX connect both ends. **Zero affordable award options.** The space is almost all Alaska, which needs Atmos points you do not have and Capital One cannot supply; the cheapest gaps to close are thousands of dollars (a 1-hour hop at 27,500 to 35,000 miles a leg). The cash through-ticket is the realistic option ($1,067 economy, $1,809 business, holiday fares). Economy awards are cheap in miles (RNO>SEA from 5,000) but roughly 70,000 Atmos points for the round trip would cost far more to buy than the cash fare (rough, from retail estimates).
  - [x] Flexible travel windows (2026-09-21): `trip add --flexible` / `trip set --flexible|--exact`; default is exact dates. Flexible searches awards 3 days either side of both the outbound and return dates (`windows.py`). Neither current trip is flexible yet: the Kelowna dates were given as approximate ("~"), so `trip set 2 --flexible` is the one to try.
  - [ ] Cash fares are still for the exact dates only (the SerpApi free plan cannot afford 7 dates x 2 directions x 3 cabins per trip). If flexible trips matter, price a few sample dates, or use a date-grid source, or move to a paid plan.
  - [x] Each date is flexible on its own (2026-10-03): `--flexible-outbound`, `--flexible-return`; `--flexible` is both.
  - [x] Minimum stay of one night (2026-10-03). Still no maximum trip length: a flexible outbound and return move independently.
  - [x] Joint evaluation: pairs of options (one per trip) are assessed together on the shared balances; a `SHARED BALANCES` report section, digest line, alert and plan notes, and the advisor all use it.
  - [x] Balances are shared across trips (one pool, one set of program balances).
  - [ ] Choose the objective: fewest total miles, most total savings, or least cash spent. Today the report shows the cheapest pair by total miles and leaves the choice to you.
  - [x] Pruned to the leading 5 alert-worthy options per cabin per trip before pairing.
  - [x] Conflicts are surfaced ("with this booked, nothing affordable is left for Kelowna").
  - [ ] Not done: it compares pairs of trips only (three or more trips are checked pair by pair, not all together), only awards that would themselves alert, and it does not say which trip should get the miles.
- [ ] **Locally hosted GUI** for all of the above: trips and the `active` toggle, balances, ranked options with cash price, savings and cents per mile, and Claude's recommendation. Pick a framework (e.g. Streamlit, Flask or FastAPI). Bind to `127.0.0.1` only, since it shows balances and sits next to API keys.

## Notes

- Southwest has no alliance or transfer partners, so it's tracked for completeness but won't factor into most award searches.
- Delta, American, and United can't receive Capital One transfers. Their balances are tracked and earned separately.
- No auto-booking is planned. This is a research and alerting tool only.
