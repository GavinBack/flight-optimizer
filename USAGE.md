# How to use Flight Optimizer

Flight Optimizer watches your trips every day and tells you when there is a good way to book one, using cash or airline miles. It never books anything. You still book on the airline's own site, and it is a good idea to confirm every price there before you buy.

For each trip it looks at three cabins: economy, premium economy and business. It tells you the best option in each, and it speaks up when a better cabin gets cheap enough to be worth it.

## Before you start

You only do this once.

1. Open a terminal in the project folder and switch on the Python environment:

   ```
   source .venv/bin/activate
   ```

2. Copy `.env.example` to `.env` and fill in your keys. The file is private and never goes to GitHub.
   - `SEATS_AERO_API_KEY` finds award seats. This one is required.
   - `SERPAPI_API_KEY` finds cash prices (the free plan allows 250 searches a month).
   - `NTFY_TOPIC` is the name of your phone alert channel. Treat it like a password. Make it long and random, and subscribe to it in the ntfy phone app.
   - `ANTHROPIC_API_KEY` lets Claude write advice after each daily run. It is optional. It costs about 10 cents each time it runs.

3. Check that alerts reach your phone:

   ```
   python -m flight_optimizer test-notification
   ```

Every command below starts with `python -m flight_optimizer`. That part is left out of the examples in the tables, so `trip list` really means `python -m flight_optimizer trip list`.

## The usual routine

1. **Add your trip** (see "Managing trips" below).
2. **Enter your miles balances** with `balance`, so the app knows what you can afford.
3. **Run the report** to see what it finds right now.
4. **Let the daily check run.** A scheduled job runs `notify` at 7:00 AM every day and sends a phone alert only when there is something new. Most days you hear nothing, and that is normal.
5. **When an alert arrives**, run `plan` on the laptop to see the step-by-step booking plan.
6. **Once you book**, run `done` so the app stops searching for that trip.

## Everyday commands

| Command | What it does |
|---|---|
| `report` (or no command at all) | Prints the current picture for every active trip. Shows the best options in each cabin, cash prices and what your balances can pay for. |
| `notify` | The daily check. Sends a phone alert for anything new, then asks Claude for advice. |
| `plan [TRIP]` | Shows the saved booking plan, with the steps to follow on the airline's site. |
| `advise [TRIP]` | Asks Claude to weigh up the options for your trips. |
| `digest` | Sends the weekly summary now. |
| `done TRIP` | Marks a trip as booked. It stops being searched and stops sending alerts. |
| `test-notification` | Sends a test message to your phone. |

`TRIP` is the trip's number, as shown by `trip list`. Where it says `[TRIP]`, you can leave it out if only one trip is active. If several are active, the app asks you which one you mean.

### Options that change how a command runs

- `--top N` shows N options per trip in the report (the default is 5). Example: `--top 10 report`.
- `--fresh` skips the one-hour memory of recent searches and asks Seats.aero again. Put it before the command: `--fresh report`.

### report

```
python -m flight_optimizer
python -m flight_optimizer report --top 3
```

A full run makes about 18 Seats.aero calls. Cash prices come from a stored copy, so they cost nothing extra unless they are more than 2 days old.

### notify

```
python -m flight_optimizer notify --dry-run
```

- `--dry-run` shows what would be sent, but sends nothing and saves nothing. Use it to see what today's alert would say.
- `--no-advice` skips the Claude advice.

The scheduled job runs this for you. You only need to run it by hand to test.

### plan

```
python -m flight_optimizer plan 1
python -m flight_optimizer plan 1 --cabin business
python -m flight_optimizer plan 1 --refresh
```

- With no options it shows the latest saved plan.
- `--cabin` shows just one cabin: `economy`, `premium_economy` or `business`.
- `--refresh` searches again and builds a new plan for each cabin that has an option you can afford. A first run like this can use about 45 Seats.aero calls.

Plans are saved as text files in the `plans/` folder.

Each plan ends with a **miles vs cash** table in cents per mile, so the ways of paying can be compared on one scale:

- **Cash:** what each mile replaces. This is the cash fare minus the award's taxes, divided by the miles. For premium cabins the plan also shows the same figure against economy.
- **Buy:** what buying each program's miles would cost per mile. It uses a live sale if one is cheaper, shows the retail price beside it, and counts purchases rounded up to the block size.
- **Capital One:** the same miles moved from Capital One, valued at 1 cent a mile (the travel-eraser rate).
- **Total:** buying every mile plus the taxes, against the cash fare. Judge by this line. One program can cost less per mile than cash while the trip as a whole does not.

Purchase prices are estimates. A program that cannot be bought, or a purchase over the yearly limit, makes the total a minimum (marked `+`). The backup options also show their cents per mile and what buying every mile would cost.

### advise

```
python -m flight_optimizer advise --last
python -m flight_optimizer advise --dry-run
python -m flight_optimizer advise 2
```

- `--last` shows the most recent saved advice. It is free.
- `--dry-run` shows exactly what would be sent to Claude and spends nothing.
- With no options it asks Claude and costs about 10 cents.

Advice is saved in the `advice/` folder. It is never sent to your phone, because it talks about your balances and ntfy.sh is a public server. Claude gives its opinion only. The app does all the maths.

## Managing trips

### See your trips

```
python -m flight_optimizer trip list
```

This shows each trip's number, whether it is active, its route, its dates, whether the dates are flexible and which hub airports it searches through.

### Add a trip

```
python -m flight_optimizer trip add "Costa Rica" RNO SJO --depart 2027-01-09 --return 2027-01-16 --gateways LAX
```

- The name comes first, then the airport you leave from, then where you are going.
- Give the outbound date one of two ways. Dates are written year-month-day.
  - `--depart` (the usual way): the day the flight there **leaves**.
  - `--arrive` (only if you choose it): the day you must **arrive**. The flight there may leave up to 1 day earlier (an evening or overnight flight), but it must land on that day.
- `--return` is the day the flight home **leaves**.
- Leave out `--return` for a one-way trip.
- `--gateways` lists the hub airports to route through. Many small airports have no direct award route, so the app searches through big hubs instead (for example RNO to LAX, then LAX to SJO).
- `--flexible` searches awards up to 3 days either side of each date. With `--arrive`, that is the day the flight there lands. Every option includes at least one night at your destination: the flight home never leaves on the day you land. Cash prices are always checked only for flights leaving on your exact dates.
- `--inactive` saves the trip without searching it yet.

A new trip starts searching at the next report or daily check.

### Change a trip

```
python -m flight_optimizer trip set 1 --depart 2027-01-10 --return 2027-01-17
python -m flight_optimizer trip set 2 --flexible
python -m flight_optimizer trip set 2 --inactive
```

You can change these:

- `--name` renames the trip.
- `--depart` and `--return` change the dates. `--arrive` changes the outbound date and makes it an arrival date; `--depart` makes it a departure date again. `--one-way` drops the return leg. Changing a date clears the saved cash price history for that trip (switching between `--depart` and `--arrive` on the same date does not).
- `--flexible` or `--exact` turns the 3-day windows on or off.
- `--active` or `--inactive` starts or stops searching and alerts. An inactive trip sends no alerts at all, however good the deal.

### Set a cash price target

```
python -m flight_optimizer trip target 1
python -m flight_optimizer trip target 1 business 2500
python -m flight_optimizer trip target 1 business --clear
```

A target is the round-trip cash price you would happily pay. You get an alert when the cash fare reaches it. You can set one for each cabin. With just a trip number, the command shows the targets you have set.

Separately, the app alerts you on its own when an economy fare drops to a new 30-day low that is also clearly below its usual price. That needs about a week of price history before it can start.

### Choose the hub airports

```
python -m flight_optimizer gateways 1
python -m flight_optimizer gateways 1 --add DEN
python -m flight_optimizer gateways 1 --remove DEN
python -m flight_optimizer gateways 1 --set LAX SEA
```

With just a trip number, it shows the current hubs and how many Seats.aero calls each run costs. Each extra hub adds calls, so only keep hubs that produce real options.

## Your miles balances

```
python -m flight_optimizer balance
python -m flight_optimizer balance "Capital One" 120000 delta 15000
```

- With nothing after it, `balance` shows what is saved.
- To change balances, give a program name and a number, and repeat for as many programs as you like. Short names work, such as `delta`, or the first few letters of a name.
- It is all or nothing. If any name is not understood, nothing is saved.

Balances stay on your computer, in the local database. They are not stored in git and never appear in phone alerts.

**About Capital One miles.** These can move 1 to 1 into Air Canada Aeroplan, Avianca LifeMiles, Flying Blue, British Airways Avios, Qantas and Virgin Atlantic, and 5 to 3 into JetBlue. Transfers cannot be undone, and some are slow (Qantas can take up to 36 hours, and JetBlue 2 to 5 days). They cannot be used for United, Alaska, Delta, American or Southwest, so those balances are counted separately. The app treats Capital One miles as a shared flexible pool and pays a program's shortfall from it.

## Buy-miles sales

The app reads deal websites for sales on buying miles, and it alerts you when a sale makes buying miles cheap enough to cover a gap for an active trip.

```
python -m flight_optimizer promos
python -m flight_optimizer promos --no-fetch
python -m flight_optimizer promos add delta --bonus 50 --ends 2026-10-15 --tier 5000-25000:30 --tier 25000+:50
python -m flight_optimizer promos remove delta
```

- `promos` lists the sales it knows about. It checks the websites first, but no more than once every 6 hours. `--no-fetch` lists without going online.
- `promos add` records a sale you saw yourself. Give either `--bonus PERCENT` (extra miles) or `--discount PERCENT` (a lower price), plus `--ends` with the last day. `--tier` describes each purchase size, and you can repeat it. `--url` adds the sale's page so you can open it from an alert.
- Without tiers, a sale is treated as a best case and never triggers an alert.
- `promos remove` deletes the sales you added by hand for one program.

## What the alerts mean

You may get these messages on your phone:

- **Award alert.** There is a good, affordable award option whose data is fresh. It shows the urgency (based on how many seats are left) and roughly what it costs in miles plus taxes and fees. Details are in `plan`.
- **Better cabin.** A cabin above the cheapest one has come within about 1.5 times the price of the cheapest way to fly.
- **Cash fare.** A fare hit your target or dropped to a new low.
- **Buy-miles sale.** A sale makes topping up your miles worthwhile.
- **Weekly digest.** A quiet summary sent every 7 days, so you know the tool is still working even when nothing else was worth an alert.
- **Flight check ...** messages. These are about the tool, not your trip. They mean something failed (a search, cash prices or the Claude advice) or that a daily run was missed because the Mac was off or asleep.

The same deal is not sent twice within 7 days unless it gets cheaper.

Award data comes from Seats.aero's stored snapshot and can be old. The app puts fresher data first and never sends an alert based on stale data.

## The daily schedule

A background job on your Mac runs `notify` every day at 7:00 AM and writes to `logs/notify.log`. It only runs while the Mac is awake.

Stop it:

```
launchctl bootout gui/$(id -u)/com.gavinback.flightoptimizer.notify
```

Start it again:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gavinback.flightoptimizer.notify.plist
```

The job remembers the exact folder where this project lives. If you move the project or the `.venv` folder, the job has to be set up again. The job does not read your shell settings, so `ANTHROPIC_API_KEY` has to be in `.env` for the daily run to include advice.

## What costs money or quota

| Thing | Limit or cost |
|---|---|
| Seats.aero | 1,000 calls a day on the Pro plan. A report uses about 18. |
| SerpApi cash prices | 250 searches a month on the free plan. Prices are stored and refreshed every 2 days. Each round-trip trip uses about 90 a month. |
| Claude advice | About 10 cents each time it asks. It skips a day when nothing has changed. |
| ntfy | Free. |

## If something looks wrong

- **No alerts.** Check that the trip is active (`trip list`). Then look at `logs/notify.log`. Silence often just means nothing is worth booking yet.
- **A trip shows no options.** Try adding a hub with `gateways`, or widen the dates with `trip set N --flexible`.
- **A price looks off.** Confirm it on the airline's own site. Taxes are converted to dollars at rough fixed rates, and the app marks those amounts as estimates.
- **Phone gets nothing.** Run `test-notification`, and make sure the topic in `.env` matches the one in the ntfy app.
- **Run the tests.** `pytest` checks the code. It makes no live calls and costs nothing.
