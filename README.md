# Travel Rewards Optimizer

A personal tool that watches for the best way to book a trip — cash or miles — across multiple loyalty programs, and alerts me when something worth booking shows up. Goal is to stop manually re-checking six different sites every time I want to travel.

## Core idea

I enter a trip once (route, dates, points/miles balances). The app checks daily and tells me when there's a good option — I still book manually, this just does the watching.

**Notifications only fire while I'm actively searching for a trip.** Each saved trip has an `active` flag (default off). The app can keep balances/checklist data around indefinitely, but it only runs the daily check — and only sends alerts — for trips marked active. This is a deliberate choice: I don't want general "there's a deal happening" noise when I'm not currently trying to book something. When a trip is marked inactive (booked, or shelved), alerts for it stop immediately.

## Inputs

- Starting location
- Destination
- Travel dates (departure / return)
- Active? (yes/no — gates whether this trip is checked/alerted on at all; see note above)
- Current balances:
  - Capital One miles
  - Delta SkyMiles
  - American AAdvantage
  - United MileagePlus
  - Qantas Frequent Flyer
  - Alaska/Hawaiian (Atmos Rewards)
  - Southwest Rapid Rewards

## Accounts to set up

Free to create, no balance required to start — needed so the app (and I) can search live award space.

| Program | Alliance | Reachable via Capital One transfer? | Status |
|---|---|---|---|
| Avianca LifeMiles | Star Alliance | Yes (1:1, instant) | ☐ |
| British Airways Executive Club (Avios) | Oneworld | Yes (1:1, instant) | ☐ |
| Air France/KLM Flying Blue | SkyTeam | Yes (1:1, instant) | ☐ |
| Qantas Frequent Flyer | Oneworld | Yes (1:1, instant) | ☐ |
| United MileagePlus | Star Alliance | No — earn directly / credit card | ☐ |
| Delta SkyMiles | SkyTeam | No — earn directly / credit card | ☐ |
| American AAdvantage | Oneworld | No — earn directly / credit card | ☐ |
| Alaska/Hawaiian (Atmos Rewards) | Oneworld | No — earn directly / Bilt / Marriott | ☐ |
| Southwest Rapid Rewards | None (no alliance) | No | ☐ |

**Note:** Southwest has no airline transfer/alliance partners, so it only surfaces Southwest's own domestic + limited international routes — not useful for most award routing outside the US/Caribbean/Central America. Keeping it in the balance tracker for completeness, but it won't factor into most award searches.

## Data sources

- **Award availability:** [Seats.aero](https://seats.aero) API — aggregates live award space across LifeMiles, United, Delta, American, Avios, Flying Blue, and others. Avoids scraping airline sites directly (against ToS, breaks often).
- **Cash fares:** Skyscanner, Amadeus, or Kiwi.com API (all have usable free tiers).

## Architecture (draft)

- **Scheduler:** daily cron job (cloud function — AWS Lambda / Cloudflare Workers, or just a local scheduled script)
- **Data layer:** SQLite — stores trips being watched, balances, and alert history (so it doesn't re-alert on the same fare)
- **Logic layer:** rules engine — compares live results against my thresholds (e.g. "business class ≤ 60K miles round trip" or "cash economy < $X")
- **Notification:** Pushover, ntfy.sh, or email/SMS via Twilio

## Alert logic (draft rules)

- Alert if a business-class award is bookable within my current balance for the entered program(s)
- Alert if cash fare drops below a set threshold
- Alert if new award space opens up on a previously "sold out" route/date
- Don't re-alert on something already surfaced in the last N days unless the price/availability improves

## Buy-miles promo alerts

A separate alert type from award/cash monitoring — watches for discounted mile-purchase sales (e.g. LifeMiles bonus-mile promos) that make it cheaper to top up a balance to reach a business-class redemption.

- **Data source:** no clean API exists for this — promos are announced via program emails/blogs, not searchable inventory. Realistic approach: scrape/poll an RSS feed or a couple of deal-tracking sites (The Points Guy, Doctor of Credit, One Mile at a Time) filtered for "buy miles" + program name.
- **Logic:** convert promo bonus % into effective cost-per-mile, then alert only if:
  1. There's at least one trip marked `active` (see Core idea above) — no active trip, no alert, regardless of how good the sale is
  2. Effective ¢/mile is below a set threshold for that program (LifeMiles' normal price is ~2.5–3¢, so a good sale might be ≤1.5¢), **and**
  3. I'm short of the miles needed for that active trip's business-class (or better) redemption
- **Open question:** may need to hardcode "normal price" baselines per program manually, since these aren't published in any structured feed

## Search cadence

Once daily is sufficient for this use case — no need for real-time polling. Only trips marked `active` are included in the daily run.

## Known limitations / open questions

- Seats.aero free tier may not support the search volume needed for continuous daily monitoring across all these programs — check pricing tiers before committing
- Delta, American, and United don't accept Capital One transfers directly, so those balances have to be tracked and earned separately (co-branded cards, direct spend, etc.)
- Award pricing is increasingly dynamic (not fixed charts) for several programs — thresholds may need periodic re-tuning
- No plans yet for auto-booking; this is a research/alerting tool only

## Roadmap

- [ ] v1: single route, single alert channel, manual balance entry
- [ ] v2: multiple saved trips, historical price tracking
- [ ] v3: simple web UI instead of config file/script
- [ ] v4 (maybe): auto-suggest which program to transfer Capital One points to, given live award pricing across all reachable partners