# TODO

Derived from `README.md`. Ordered roughly by dependency: decisions first, then accounts, then build.

## 0. Decisions to make first

These block design choices further down.

- [ ] Check Seats.aero pricing and API limits: does the free tier cover a daily check across all programs? (README open question)
- [ ] Pick a cash-fare source: Skyscanner, Amadeus, or Kiwi.com. Compare free-tier limits and route coverage.
- [ ] Pick one notification channel for v1: Pushover, ntfy.sh, or email/SMS via Twilio.
- [ ] Pick where the scheduler runs: cloud function (Lambda / Cloudflare Workers) or a local scheduled script.
- [x] Pick a language and stack: Python 3.12 with a `.venv` virtual environment, SQLite for storage.
- [ ] Decide how "good" is measured, since this drives the rules engine:
  - [ ] Cash threshold per trip (the "$X" in the README)
  - [ ] Max miles for business class, per program
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
- [ ] Seats.aero account and API key
- [ ] Cash-fare API key (whichever source is chosen above)
- [ ] Notification service account/token

## 2. v1: single route, single alert channel, manual balances

- [ ] Project scaffold: repo layout, dependency management, config and secrets handling (API keys stay out of git)
- [ ] `.gitignore` for secrets, `.env`, and the SQLite database file
- [ ] SQLite schema:
  - [ ] `trips` table: origin, destination, depart date, return date, `active` flag (default off)
  - [ ] `balances` table: one row per program
  - [ ] `alert_history` table: what was alerted, when, and at what price/availability, to prevent re-alerts
- [ ] Manual balance entry for all 7 tracked programs (Capital One, Delta, AAdvantage, MileagePlus, Qantas, Atmos, Southwest)
- [ ] Seats.aero client: search award availability for one route and date range
- [ ] Cash-fare client: search fares for the same route and dates
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

## Notes

- Southwest has no alliance or transfer partners, so it's tracked for completeness but won't factor into most award searches.
- Delta, American, and United can't receive Capital One transfers. Their balances are tracked and earned separately.
- No auto-booking is planned. This is a research and alerting tool only.
