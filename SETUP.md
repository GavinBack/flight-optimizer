# Setting up Flight Optimizer

This guide takes you from a fresh copy of the code to a daily phone alert. It covers every account you need to open, every file you need to create and every command to run. For what the commands do once you are set up, see `USAGE.md`.

Flight Optimizer is an alerting tool. It never books anything. You book on the airline's own site.

## What you need

| Item | Required? | Cost | What it is for |
|---|---|---|---|
| A Mac (or any machine with Python 3.12) | Yes | | Everything runs locally. The daily schedule in step 6 is written for macOS. |
| Seats.aero Pro account and API key | **Yes** | About $10 a month | Finds award seats (booking with miles). The app does not start without this key. |
| ntfy app and a topic name | Yes for alerts | Free | Sends alerts to your phone. |
| SerpApi account and API key | Recommended | Free plan: 250 searches a month | Finds cash fares. Without it the app still runs, but skips cash prices, so awards are not compared with paying cash. |
| Anthropic API key | Optional | About 10 cents each time it runs | Claude writes advice after each daily run. |
| Airline loyalty accounts | Only to book | Free | The app does not use them. You need them to actually transfer miles and book. See step 8. |

## 1. Open your accounts and get your keys

### Seats.aero (required)

1. Create an account at [seats.aero](https://seats.aero) and subscribe to **Pro**. The free tier has no API access.
2. Find your API key in your account settings and copy it. Pro allows 1,000 API calls a day, which is enough for a few trips. A trip with one hub airport uses up to 6 searches per run, and each extra hub adds up to 4 more. The `trip add` command prints the exact figure.

Check the current price and API limits on the site before you subscribe. If you have no active trips, you can cancel until you need it again.

### SerpApi (recommended)

1. Create a free account at [serpapi.com](https://serpapi.com).
2. Copy your API key from the dashboard.

The free plan is 250 searches a month. The app caches cash fares for 2 days, and one round-trip trip uses about 90 searches a month across the three cabins. Two trips use about 180. A third trip would go over the free plan.

### ntfy (needed for phone alerts)

1. Install the **ntfy** app on your phone (iOS or Android).
2. Make up a topic name. On the public ntfy.sh server the topic name works as a password, so anyone who knows it can read your alerts and send you fake ones. Make it long and random. This generates one:

   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

3. In the app, subscribe to that topic name. Keep the name for step 3.

Alerts never include your balances, but they do name your routes and prices. If you would rather not use a public server, you can host your own and set `NTFY_SERVER` (see step 3).

### Anthropic (optional)

1. Create an account at [console.anthropic.com](https://console.anthropic.com).
2. Add a payment method and **set a monthly spend limit** there.
3. Create an API key and copy it.

Claude asks for advice only when something has changed since its last answer, so it costs less than 10 cents a day. Without this key, everything else works, and the daily run prints "Advice: skipped".

## 2. Install the code

You need Python 3.12 and git.

```
git clone git@github.com:GavinBack/flight-optimizer.git "Flight Optimizer"
cd "Flight Optimizer"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The repository is private, so the clone only works if its owner has given your GitHub account access. If you received the code as a folder or zip file instead, skip the clone and just open a terminal in that folder.

Every command in this guide runs from the project folder with the environment switched on (`source .venv/bin/activate`). The app looks for `.env` and the database in the folder you run it from, so running it from somewhere else fails with "SEATS_AERO_API_KEY is not set".

## 3. Create your `.env` file

`.env` holds your keys. It is private and git-ignored. Copy the template and lock it down:

```
cp .env.example .env
chmod 600 .env
```

Then open `.env` and fill in the values:

```
SEATS_AERO_API_KEY=your-seats-aero-key
SERPAPI_API_KEY=your-serpapi-key
NTFY_TOPIC=your-long-random-topic
ANTHROPIC_API_KEY=your-anthropic-key
```

Leave a key empty to turn that feature off. Two more settings are optional:

- `NTFY_SERVER` points at your own ntfy server instead of `https://ntfy.sh`.
- `ANTHROPIC_MODEL` picks a different Claude model. The default is `claude-sonnet-5`.

`DATABASE_PATH` already defaults to `flight_optimizer.db`, in the project folder. The database is created the first time you run a command. It holds your trips and balances and is git-ignored, so **it has no backup**. Copy it somewhere safe now and then.

Never commit `.env`, and never paste your ntfy topic or keys into a message, issue or screenshot.

## 4. Check the install

Run the tests. They use fake network responses, so they spend no API calls:

```
pytest
```

Then check that alerts reach your phone:

```
python -m flight_optimizer test-notification
```

You should get a test message within seconds. If not, see "Troubleshooting".

## 5. Tell the app about your trips and miles

The database starts empty. You need to add at least one trip and your balances.

**Add a trip.** The name comes first, then where you leave from and where you are going:

```
python -m flight_optimizer trip add "Costa Rica" RNO SJO --depart 2027-01-09 --return 2027-01-16 --gateways LAX
```

- Dates are year-month-day and are the days the flights leave. If you must arrive by a certain day instead, use `--arrive` in place of `--depart`. Leave out `--return` for a one-way trip.
- `--gateways` lists hub airports to route through. Seats.aero only knows airport pairs that airlines publish, so a small airport often has no direct award route. Routing RNO to LAX, then LAX to SJO, finds options a direct search misses. Each extra hub adds Seats.aero calls, so start with one or two big hubs near your origin.
- Add `--flexible` to also search 3 days either side of each date.
- Trips start active, so the app searches and alerts on them straight away. Add `--inactive` to save one without that.

**Enter your miles balances.** The app uses these to decide what you can afford. Every program starts at 0. Give a program name and a number, and repeat for as many programs as you hold. Programs you leave out stay as they are:

```
python -m flight_optimizer balance "Capital One" 120000 delta 15000 aeroplan 0
```

The programs are `capital_one`, `delta`, `american`, `united`, `qantas`, `atmos`, `southwest`, `aeroplan`, `lifemiles`, `flyingblue`, `british`, `virginatlantic` and `jetblue`. Short names and the first few letters work. Run `balance` alone to see what is saved.

Capital One miles are treated as a shared pool that can move 1 to 1 into Aeroplan, LifeMiles, Flying Blue, Avios, Qantas and Virgin Atlantic (5 to 3 into JetBlue). If you hold no Capital One miles, leave that at 0 and enter the balances of the programs you do hold.

**Optional: set a cash price target.** You get an alert when a cabin's round-trip cash fare reaches it:

```
python -m flight_optimizer trip target 1 business 2500
```

## 6. Run it once by hand

```
python -m flight_optimizer report
```

This searches every active trip and prints the best option in each cabin. It spends Seats.aero calls (the owner's two trips use about 18 per run) and, on a first run, some SerpApi searches for cash fares. It sends nothing to your phone.

To see what the daily check would say, without sending or saving anything:

```
python -m flight_optimizer notify --dry-run
```

Most days the daily check sends nothing, and that is normal. It only speaks when something new is worth booking, and it never alerts on award data more than 7 days old.

## 7. Schedule the daily check (macOS)

The daily check is `python -m flight_optimizer notify`. On a Mac, a launchd job runs it at 7:00 AM. The commands below write the job file using your current folder, so run them from the project folder:

```
mkdir -p logs ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.flightoptimizer.notify.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.flightoptimizer.notify</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PWD/.venv/bin/python</string>
    <string>-m</string>
    <string>flight_optimizer</string>
    <string>notify</string>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>$PWD/logs/notify.log</string>
  <key>StandardErrorPath</key><string>$PWD/logs/notify.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.flightoptimizer.notify.plist
```

Things to know:

- **The job does not read your shell settings**, so every key must be in `.env`. A key exported only in `.zshrc` is invisible to it.
- **The Mac must be awake or on at 7:00 AM.** If it is asleep, launchd runs the job when the Mac wakes. If it is switched off all day, that day is missed, and the next run sends a "Flight check was missed" alert.
- **The file holds absolute paths.** If you move the project folder or the `.venv`, stop the job, then repeat this step.
- **Check the result** in `logs/notify.log` after the first 7:00 AM run.
- **To run it now** (a real run that spends API calls and can send alerts): `launchctl kickstart gui/$(id -u)/com.flightoptimizer.notify`
- **To stop it:** `launchctl bootout gui/$(id -u)/com.flightoptimizer.notify`

On Linux, use a cron entry or a systemd timer that runs the same `notify` command from the project folder with the virtual environment's Python. On Windows, use Task Scheduler.

## 8. Open loyalty accounts (only when you are ready to book)

The app does not need these. Open one when an alert points at that program, since you must have the account to transfer miles into it and book. They are free.

| Program | Why you might need it |
|---|---|
| Air Canada Aeroplan | Where many awards turn up. Capital One transfers here 1 to 1. |
| Avianca LifeMiles | Capital One transfers 1 to 1. |
| Air France/KLM Flying Blue | Capital One transfers 1 to 1. |
| British Airways Executive Club (Avios) | Capital One transfers 1 to 1. |
| Qantas Frequent Flyer | Capital One transfers 1 to 1, and it can take up to 36 hours. |
| Virgin Red and Virgin Atlantic Flying Club | Capital One transfers via Virgin Red, in about 1 business day. |
| JetBlue TrueBlue | Capital One transfers 5 to 3, and it takes 2 to 5 days. |
| United, Delta, American, Alaska (Atmos), Southwest | Cannot be topped up from Capital One. You need to hold miles in these directly. |

Transfers cannot be undone, so confirm the award and its price on the airline's own site before you move any miles. The app is a guide, and its figures are approximations.

## 9. Check the built-in assumptions

A few numbers in the code reflect the original owner's choices, and you may want to change them. They sit in named constants near the top of each file:

| Setting | File | What it does |
|---|---|---|
| `MILE_VALUE_CENTS` (1.0) | `flight_optimizer/cabins.py` | What a mile is worth when comparing an award with a cash fare. |
| `UPGRADE_MAX_MULTIPLE` (1.5) | `flight_optimizer/cabins.py` | How much more than the cheapest option a better cabin can cost before the app flags it. |
| `USD_PER_UNIT` | `flight_optimizer/taxes.py` | Approximate exchange rates for award taxes. Update them when they drift. |
| `RETAIL_PRICES` | `flight_optimizer/buying.py` | Estimated price of buying miles at each program. |
| `CAPITAL_ONE_TRANSFERS` and `TRANSFER_TIMES` | `flight_optimizer/affordability.py` | Which programs a shortfall can be topped up from, and how long each transfer takes. Edit them if you hold points in a different bank program. |

The tool is built around Capital One miles, since that is the bank program its owner holds. If yours is elsewhere, the affordability rules will need adapting.

## Files the app creates

All of these stay on your machine and are git-ignored, because they hold your balances and trip details:

| Path | What it holds |
|---|---|
| `.env` | Your API keys and ntfy topic. |
| `flight_optimizer.db` | Trips, balances, alert history and caches. Back it up yourself. |
| `plans/` | Saved booking plans (`plan` shows them). |
| `advice/` | Saved Claude advice (`advise --last` shows it). |
| `logs/notify.log` | Output of the scheduled daily run. |

## Troubleshooting

- **"SEATS_AERO_API_KEY is not set."** You are not in the project folder, or `.env` is missing or the key line is empty.
- **The test message never arrives.** Check that the app is subscribed to exactly the topic in `.env`, with no extra spaces. Then check that the phone is online. If `NTFY_SERVER` is set, make sure it is right.
- **"NTFY_TOPIC is not set."** Add it to `.env`. `notify` needs it to send anything.
- **The report says "Cash prices skipped".** `SERPAPI_API_KEY` is empty.
- **The daily job never runs.** Run `launchctl list | grep flightoptimizer` to see whether it is loaded. Check that `logs/` exists and that the paths in the plist are right. Then read `logs/notify.log`.
- **Every option is "stale".** Seats.aero serves cached data, and some routes go unrefreshed for weeks. The app deliberately stays silent on stale data. A weekly digest is sent so that silence is not ambiguous.
- **Seats.aero returns an error about limits.** You have used the 1,000 calls for the day. Wait for it to reset, and avoid `--fresh` and `plan --refresh` until then.
