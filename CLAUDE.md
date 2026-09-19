# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Early stage: the repo currently contains only planning docs and setup files (`README.md`, `todo.md`, `requirements.txt`, `.env.example`, `.gitignore`). There is no application code, test suite, or lint config yet. `README.md` is the product spec and its architecture section is explicitly a draft. `todo.md` tracks open decisions and the build order, so check it before assuming something has been decided.

Remote: `git@github.com:GavinBack/flight-optimizer.git` (SSH, private), branch `main`.

## What this is

A personal alerting tool (not a booking tool, and auto-booking is out of scope) that checks daily for the best way to book a trip, with cash or miles across several loyalty programs, and notifies the owner when something is worth booking.

## Commands

Python 3.12 in a local virtualenv:

```
python -m venv .venv                      # already created locally
source .venv/bin/activate
pip install -r requirements.txt
pytest                                    # all tests (none exist yet)
pytest path/to/test_file.py::test_name    # a single test
```

No linter, formatter, or entry-point script has been chosen yet. Don't invent one. Add it to this file when it's introduced.

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
- **Tracked programs:** Capital One, Delta SkyMiles, American AAdvantage, United MileagePlus, Qantas, Alaska/Hawaiian (Atmos), Southwest. Capital One miles transfer 1:1 to LifeMiles, Avios, Flying Blue and Qantas. Delta, American and United can't receive transfers, so their balances are tracked separately.
- **Southwest** has no alliance or transfer partners. It is tracked for completeness but should not factor into most award searches.
- Award pricing is increasingly dynamic, so thresholds are expected to need periodic re-tuning and shouldn't be hardcoded deep in logic.

## Secrets and local data

API keys live in `.env` (git-ignored). `.env.example` is the tracked template, so update it whenever a new variable is introduced. The SQLite database (`*.db`, `*.sqlite*`) is git-ignored because it holds personal balance and trip data.
