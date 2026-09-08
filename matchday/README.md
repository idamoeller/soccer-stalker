# Matchday — live gotSport score scraper (Mac-native)

The GitHub-Actions poller reads gotSport's **per-team feed**, which can lag up to
~24 hours before a played game's score shows up. The **bracket/schedule page**,
by contrast, updates the instant a score is entered. Matchday reads that page so
you get scores while the games are still happening — and pushes an ntfy alert
when a followed team's score changes.

The catch: those schedule pages sit behind an *invisible* reCAPTCHA v3. A real
browser passes it silently; plain `requests` and headless browsers get blocked.
So this runs in two pieces:

- **`refresh_cookie.py`** — opens a real (but off-screen) Chromium once a day,
  passes reCAPTCHA v3, and saves the gotSport session cookie to `cookies.json`.
  That cookie then works from plain `requests` for ~27 hours.
- **`poll_scores.py`** — every few minutes, reuses the saved cookie (no browser)
  to fetch the schedule pages for followed teams whose games are near kickoff,
  reads the fresh scores, updates Supabase, and fires ntfy on any change.

Scope: **gotSport only** for now (GA / Aspire / Inspire / DPL / NAL and gotSport
tournaments). ECNL/RL live scores are a different site — later.

---

## One-time setup

**1. Create the virtualenv and install dependencies** (downloads a bundled
Chromium into the venv — isolated from your Safari/Chrome):

```bash
cd /Users/idamoeller/coding-projects/soccer-stalker/matchday
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

**2. Create your `.env`** from the template and paste your secret key:

```bash
cp .env.example .env
```

Then open `.env` and paste your Supabase **service** key (`sb_secret_…`) after
`SUPABASE_KEY=`. It stays on your Mac only (gitignored). `SUPABASE_URL` and a
proposed `NTFY_TOPIC` are pre-filled — subscribe to that topic in the **ntfy**
app on your phone so you get the pushes.

**3. Add the score-protection trigger** in the Supabase SQL editor — paste and
run the contents of `score_protection.sql`. It stops the lagging feed from ever
nulling out a freshly-scraped score.

**4. Get the first cookie** (a Chromium window flashes off-screen for a few
seconds — that's expected):

```bash
.venv/bin/python refresh_cookie.py
.venv/bin/python refresh_cookie.py --show      # confirm it saved
```

**5. Smoke-test the scraper** without touching Supabase — fetch and parse one
event's schedule (56498 = GA Aspire; add an age + gender to narrow it):

```bash
.venv/bin/python poll_scores.py --probe 56498 13 f
```

You should see a list of games with scores. Then try a real (window-gated) run:

```bash
.venv/bin/python poll_scores.py            # says "nothing to do" if no game is near
.venv/bin/python poll_scores.py --force    # check all of today's games now
```

**6. Schedule both jobs** with launchd:

```bash
cp launchd/com.idamoeller.soccer-cookie.plist ~/Library/LaunchAgents/
cp launchd/com.idamoeller.soccer-poll.plist   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.idamoeller.soccer-cookie.plist
launchctl load ~/Library/LaunchAgents/com.idamoeller.soccer-poll.plist
```

- `soccer-cookie` runs daily at **3:00am** (so no browser pops up during the
  day). Your Mac must be awake at 3am for it to run then; otherwise launchd runs
  it at the next wake.
- `soccer-poll` runs every **3 minutes**, but only actually hits gotSport when a
  followed team's game is within a few hours of kickoff — off-hours it's just two
  tiny Supabase reads and an exit.

To stop them: `launchctl unload ~/Library/LaunchAgents/com.idamoeller.soccer-*.plist`.

---

## How it decides what to check

`poll_scores.py` pulls the `games` rows for followed gotSport teams whose kickoff
is within `LOOKBACK_HOURS` (default 5) before to `LOOKAHEAD_HOURS` (default 2)
after now — plus today's games whose time is still TBD. It buckets them by
`(event, age, gender)`, fetches each schedule page once, parses every
`div.public-match`, and matches each stored game by **team name + date**. When
the freshly-read score differs from what's stored, it updates the row and pushes
ntfy. Because the DB is updated, the next run sees no change and won't re-alert.

## Files

| file | what it is |
|------|------------|
| `refresh_cookie.py` | daily headed-Chromium pass → `cookies.json` |
| `poll_scores.py` | reuse cookie → fetch/parse schedules → update scores + ntfy |
| `score_protection.sql` | Supabase trigger: never null-overwrite a real score |
| `.env.example` | config template (copy to `.env`, add secret key) |
| `requirements.txt` | Python deps |
| `launchd/` | the two launchd plists |
| `cookies.json` | the saved cookie (gitignored, created at runtime) |
| `refresh.log` / `poll.log` | launchd output (gitignored) |

## Troubleshooting

- **`poll_scores.py` says "cookie is stale/blocked"** — run `refresh_cookie.py`
  again. If it fails to get a session cookie, run it a second time (v3 scoring is
  probabilistic). Check age anytime with `refresh_cookie.py --show`.
- **`--probe` returns 0 games** — that event/age/gender may have no games listed,
  or the cookie is stale. Try `--probe <event>` with no age/gender to see the
  whole event.
- **No ntfy pushes** — confirm `NTFY_TOPIC` in `.env` matches the topic you
  subscribed to on your phone, and that a score actually changed since last run.
- **Watch the logs**: `tail -f poll.log` and `tail -f refresh.log`.

## Notifications & multi-user note

v1 sends every followed-team score change to the single `NTFY_TOPIC` in `.env` —
perfect while you're the only user. Per-user topics (so other people on the site
get their own alerts) are a later design step.
