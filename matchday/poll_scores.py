#!/usr/bin/env python3
"""
Matchday score poller (Mac-native).

Reads which games we care about from Supabase, fetches the relevant gotSport
org_event *schedule* pages using the session cookie that refresh_cookie.py
harvested (plain `requests`, no browser), reads the freshest scores straight off
the bracket -- which update the instant a score is entered, hours before the
per-team feed the GitHub-Actions poller reads catches up -- and when a followed
team's score changes it updates the `games` table and fires an ntfy push.

    .venv/bin/python poll_scores.py                 # normal run (game-window gated)
    .venv/bin/python poll_scores.py --force         # ignore the window, check all of today's
    .venv/bin/python poll_scores.py --probe 56498 13 f   # no Supabase: fetch+parse one event page
    .venv/bin/python poll_scores.py --probe 56498        # ...without an age/gender filter

Config (.env in this folder): SUPABASE_URL, SUPABASE_KEY (the sb_secret_ key),
NTFY_TOPIC. Tunables: LOOKBACK_HOURS, LOOKAHEAD_HOURS.

Scope: gotSport only. ECNL/RL live scores are a separate site (later).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

GOTSPORT = "https://system.gotsport.com"
COOKIE_FILE = os.path.join(HERE, "cookies.json")
GROUP_HARVEST_STATE = os.path.join(HERE, ".group_harvest")   # last date we mapped division->group

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Only touch an event when one of its games is near kickoff. Wide enough to cover
# a long match plus score-entry lag; narrow enough that off-hours runs are two
# tiny Supabase reads and an exit.
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "5"))
LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "2"))
COOKIE_MAX_AGE_HOURS = float(os.environ.get("COOKIE_MAX_AGE_HOURS", "30"))

MONTHS = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
          "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}

# The team-centric columns we read/compare (one row per followed team per game).
GAME_FIELDS = ("match_id,team_id,team_name,team_score,opponent_id,opponent_name,"
               "opponent_score,is_home,match_time,match_date,event_id,event_name,division_name")

PAGE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": GOTSPORT + "/",
}


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat()


# --------------------------------------------------------------------------- #
# Supabase (REST, secret key -> bypasses RLS)
# --------------------------------------------------------------------------- #
def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def sb_get(table, params, required=True):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers(),
                         params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if required:
            raise
        print(f"  (optional table {table} unavailable: {e})")
        return []


def window_games():
    """Games worth checking right now: any gotSport game kicking off within the
    look-back/-ahead window, plus today's games whose kickoff time is still TBD."""
    n = now()
    lo = (n - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    hi = (n + timedelta(hours=LOOKAHEAD_HOURS)).isoformat()
    rows = {}
    for g in sb_get("games", {"select": GAME_FIELDS, "source": "eq.gotsport",
                              "and": f"(match_time.gte.{lo},match_time.lte.{hi})"}):
        rows[(g["match_id"], g["team_id"])] = g
    today = datetime.now().strftime("%Y-%m-%d")     # local date (user is ET)
    for g in sb_get("games", {"select": GAME_FIELDS, "source": "eq.gotsport",
                              "match_date": f"eq.{today}", "match_time": "is.null"}):
        rows[(g["match_id"], g["team_id"])] = g
    return list(rows.values())


def all_today_games():
    """--force: every gotSport game dated today (ignore the window)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return sb_get("games", {"select": GAME_FIELDS, "source": "eq.gotsport",
                            "match_date": f"eq.{today}"})


def watch_labels():
    """event_id -> friendly label from watch_tournaments (optional table)."""
    labels = {}
    for w in sb_get("watch_tournaments", {"select": "event_id,label"}, required=False):
        if w.get("event_id"):
            labels[str(w["event_id"])] = w.get("label")
    return labels


def patch_score(match_id, team_id, team_score, opp_score):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/games",
        headers={**sb_headers(), "Prefer": "return=minimal"},
        params={"match_id": f"eq.{match_id}", "team_id": f"eq.{team_id}"},
        json={"team_score": team_score, "opponent_score": opp_score, "updated_at": now_iso()},
        timeout=30,
    )
    if not r.ok:
        print(f"    ! score update failed {r.status_code}: {r.text[:200]}")
    r.raise_for_status()


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def load_cookies():
    if not os.path.exists(COOKIE_FILE):
        sys.exit("No cookies.json. Run refresh_cookie.py first (it opens a browser "
                 "once to pass gotSport's reCAPTCHA v3).")
    data = json.load(open(COOKIE_FILE))
    age_h = (now() - datetime.fromisoformat(data["saved_at"])).total_seconds() / 3600
    if age_h > COOKIE_MAX_AGE_HOURS:
        print(f"  WARNING: cookie is {age_h:.1f}h old (> {COOKIE_MAX_AGE_HOURS}h); "
              "it may be stale. Consider running refresh_cookie.py.")
    return data.get("cookies", {})


# --------------------------------------------------------------------------- #
# Fetch + parse the schedule page
# --------------------------------------------------------------------------- #
def event_age_gender(division_name):
    """Best-effort (age, gender) from a division name, to narrow the schedule URL.
    e.g. 'GU13 - League 2' -> ('13','f'); '2013 Girls' -> (None,'f'). None,None is
    fine -- we then fetch the whole event schedule and match by name+date."""
    if not division_name:
        return None, None
    low = division_name.lower()
    am = re.search(r"u\s*-?\s*(\d{1,2})", low)
    age = am.group(1) if am else None
    if "girl" in low or "female" in low or re.search(r"\bg\s*u?\s*\d", low):
        gender = "f"
    elif "boy" in low or "male" in low or re.search(r"\bb\s*u?\s*\d", low):
        gender = "m"
    else:
        gender = None
    return age, gender


def schedule_url(event_id, age, gender):
    base = f"{GOTSPORT}/org_event/events/{event_id}/schedules"
    if age and gender:
        return f"{base}?age={age}&gender={gender}"
    return base


def fetch_schedule(url, cookies):
    """Returns (status, html) with status in 'ok' | 'blocked' | 'empty'."""
    r = requests.get(url, headers=PAGE_HEADERS, cookies=cookies, timeout=45, allow_redirects=True)
    body = r.text or ""
    low = body.lower()
    if "verify_captchas" in r.url or r.status_code in (401, 403) or \
       ("recaptcha" in low and "public-match" not in body) or "are you a robot" in low:
        return "blocked", body
    if r.status_code != 200:
        return "blocked", body
    if "public-match" not in body:
        return "empty", body
    return "ok", body


def parse_public_matches(html):
    """Every game on the page as {home, away, home_score, away_score, date}.
    home/away carry the (H)/(A) suffix; score is the '.label' 'N - N'; date is the
    'Mon DD, YYYY' near the calendar icon."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for pm in soup.select(".public-match"):
        anchors = [a for a in pm.select("a") if "schedules?team=" in (a.get("href") or "")]
        if len(anchors) < 2:
            anchors = pm.select("a")
        home = away = None
        loose = []
        for a in anchors:
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not txt:
                continue
            if txt.endswith("(H)"):
                home = txt[:-3].strip()
            elif txt.endswith("(A)"):
                away = txt[:-3].strip()
            else:
                loose.append(txt)
        if home is None and loose:
            home = loose.pop(0)
        if away is None and loose:
            away = loose.pop(0)
        if not (home or away):
            continue

        hs = as_ = None
        for lab in pm.select(".label"):
            m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", lab.get_text(strip=True))
            if m:
                hs, as_ = int(m.group(1)), int(m.group(2))
                break

        block = pm.get_text(" ", strip=True)
        dm = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", block)
        date = (f"{dm.group(3)}-{MONTHS[dm.group(1)]}-{int(dm.group(2)):02d}"
                if dm and dm.group(1) in MONTHS else None)

        out.append({"home": home, "away": away, "home_score": hs, "away_score": as_, "date": date})
    return out


# --------------------------------------------------------------------------- #
# Matching a stored game to a parsed one, by team NAME + date
# --------------------------------------------------------------------------- #
def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def name_match(a, b):
    if not a or not b:
        return False
    return a == b or a in b or b in a


def find_scores(game, parsed):
    """Freshest (team_score, opponent_score) for this stored game from the parsed
    page, or (None, None) if not found / not yet played. Order-independent on the
    two names; date must agree when both are known."""
    tn, on = norm(game["team_name"]), norm(game["opponent_name"])
    gd = game.get("match_date")
    fallback = None
    for pm in parsed:
        hn, an = norm(pm["home"]), norm(pm["away"])
        exact = ({tn, on} == {hn, an})
        loose = ((name_match(tn, hn) and name_match(on, an)) or
                 (name_match(tn, an) and name_match(on, hn)))
        if not (exact or loose):
            continue
        if gd and pm["date"] and pm["date"] != gd:
            continue
        # our team's score is the home or away score depending on which side we are
        if name_match(tn, hn):
            ts, os_ = pm["home_score"], pm["away_score"]
        elif name_match(tn, an):
            ts, os_ = pm["away_score"], pm["home_score"]
        else:
            continue
        if exact:
            return ts, os_
        # Generic stored names (e.g. "GA 13/14") can loosely match several rows on
        # the same date -- one played, another still blank. A real, played score
        # always wins over a blank; otherwise keep the first thing we found.
        if ts is not None and os_ is not None:
            fallback = (ts, os_)
        elif fallback is None:
            fallback = (ts, os_)
    return fallback if fallback else (None, None)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def game_url(game):
    """Tap-to-open target: this game's live gotSport schedule page, age+gender
    filtered -- the freshest public view of the score. Mirrors the website's
    game-row deep link. Returns None if we can't build one (alert still sends)."""
    ev = str(game.get("event_id") or "").strip()
    if not ev:
        return None
    age, gender = event_age_gender(game.get("division_name"))
    return schedule_url(ev, age, gender)


def notify(game, ts, os_, label):
    name = game["team_name"]
    opp = game["opponent_name"]
    letter = "W" if ts > os_ else "L" if ts < os_ else "T"
    title = f"{name} {ts}-{os_} {opp}"
    where = f" · {label}" if label else (f" · {game.get('event_name')}" if game.get("event_name") else "")
    message = f"{letter} {ts}-{os_} vs {opp}{where}"
    print(f"    ALERT -> {title} | {message}")
    if not NTFY_TOPIC:
        return
    # ⚽ leads the alert (the "soccer" tag renders as an emoji before the title).
    # "Click" makes tapping the alert open the live page.
    headers = {"Title": title, "Tags": "soccer", "Priority": "high"}
    url = game_url(game)
    if url:
        headers["Click"] = url
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"    (ntfy failed: {e})")


# --------------------------------------------------------------------------- #
# Probe mode (no Supabase): fetch + parse one event, print the games
# --------------------------------------------------------------------------- #
def probe(event_id, age=None, gender=None):
    cookies = load_cookies()
    url = schedule_url(event_id, age, gender)
    print(f"GET {url}")
    status, html = fetch_schedule(url, cookies)
    print(f"status: {status} ({len(html)} bytes)")
    if status == "blocked":
        print("  -> cookie looks stale/blocked. Run refresh_cookie.py.")
        return
    parsed = parse_public_matches(html)
    print(f"parsed {len(parsed)} games:")
    for p in parsed:
        score = f"{p['home_score']}-{p['away_score']}" if p["home_score"] is not None else "—"
        print(f"  {p['date']}  {p['home']}  {score}  {p['away']}")


# --------------------------------------------------------------------------- #
# Division -> schedule "group" id harvest (for deep-linking a game to its exact
# division page). The group id lives only on the captcha-gated event page, so we
# read it here (real cookie) and cache it in `event_groups` for the web app.
# --------------------------------------------------------------------------- #
def parse_group_map(html):
    """(age:int, conference-label:lower) -> group_id, parsed off an event's base
    schedule page, which nests age panels -> conference rows -> Schedule/Results
    links carrying ?group=<id>."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        if "group=" not in a["href"] or "schedule" not in a.get_text().lower():
            continue                                          # the Schedule link (skip its Results twin)
        m = re.search(r"group=(\d+)", a["href"])
        if not m:
            continue
        gid = m.group(1)
        agenode = a.find_parent(class_="age-group")
        age = None
        if agenode:
            am = re.search(r"group-u(\d+)", " ".join(agenode.get("class") or []))
            age = int(am.group(1)) if am else None
        row = a.find_parent(class_="row")
        label = ""
        if row:
            label = re.sub(r"\b(Schedule|Results)\b", "", re.sub(r"\s+", " ", row.get_text(" ")).strip()).strip()
        if age and label:
            out[(age, label.lower())] = gid
    return out


def _conf_of(division_name):
    """'U13G New England' -> 'New England' (strip the leading age/gender token)."""
    return re.sub(r"^\s*U?\s*\d{1,2}\s*[GBM]?\b\s*", "", division_name or "", flags=re.I).strip()


def _tracked_divisions():
    """event_id -> {division_name, ...} across all stored gotSport games."""
    out = {}
    for g in sb_get("games", {"select": "event_id,division_name", "source": "eq.gotsport"}, required=False):
        ev = str(g.get("event_id") or "").strip()
        dn = (g.get("division_name") or "").strip()
        if ev and dn:
            out.setdefault(ev, set()).add(dn)
    return out


def harvest_group_map(cookies):
    """Map each gotSport division_name -> its exact schedule `group` id and upsert
    into event_groups. Best-effort; returns the number of rows written."""
    by_event = _tracked_divisions()
    if not by_event:
        print("  group harvest: no gotSport games yet, nothing to map.")
        return 0
    rows = []
    for ev, divisions in by_event.items():
        status, html = fetch_schedule(schedule_url(ev, None, None), cookies)
        if status == "blocked":
            print("  group harvest: cookie blocked -> run refresh_cookie.py; skipping.")
            return 0
        gm = parse_group_map(html)
        if not gm:
            print(f"  group harvest: event {ev} -> no groups on page (skipped)")
            continue
        matched = 0
        for dn in divisions:
            am = re.search(r"(\d{1,2})", dn)
            gid = gm.get((int(am.group(1)), _conf_of(dn).lower())) if am else None
            if gid:
                rows.append({"event_id": ev, "division_name": dn, "group_id": gid, "updated_at": now_iso()})
                matched += 1
        print(f"  group harvest: event {ev} -> {matched}/{len(divisions)} divisions mapped ({len(gm)} groups on page)")
        time.sleep(0.4)
    for k in range(0, len(rows), 200):
        r = requests.post(f"{SUPABASE_URL}/rest/v1/event_groups",
                          headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                          params={"on_conflict": "event_id,division_name"}, json=rows[k:k + 200], timeout=60)
        if not r.ok:
            print(f"  ! event_groups upsert failed {r.status_code}: {r.text[:200]}")
    print(f"  group harvest: upserted {len(rows)} (event,division)->group rows.")
    return len(rows)


def maybe_harvest_group_map(cookies):
    """Run the harvest at most once per local day (state file); never fatal."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if os.path.exists(GROUP_HARVEST_STATE) and open(GROUP_HARVEST_STATE).read().strip() == today:
            return
    except Exception:
        pass
    try:
        harvest_group_map(cookies)
        open(GROUP_HARVEST_STATE, "w").write(today)
    except Exception as e:
        print(f"  group harvest skipped: {e}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(force=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_KEY must be set in .env.")
    cookies = load_cookies()
    maybe_harvest_group_map(cookies)   # once/day: refresh division->group deep-link map

    games = all_today_games() if force else window_games()
    if not games:
        print("No gotSport games in the window right now. Nothing to do.")
        return
    labels = watch_labels()

    # Bucket games by (event, age, gender) so each distinct schedule page is
    # fetched exactly once, then scored against every game in that bucket.
    buckets = {}
    for g in games:
        ev = str(g.get("event_id") or "").strip()
        if not ev:
            continue
        age, gender = event_age_gender(g.get("division_name"))
        buckets.setdefault((ev, age, gender), []).append(g)

    print(f"{len(games)} game(s) in window across {len(buckets)} schedule page(s).")
    changed = 0
    for (ev, age, gender), bucket in buckets.items():
        url = schedule_url(ev, age, gender)
        status, html = fetch_schedule(url, cookies)
        print(f"  event {ev} age={age} gender={gender}: {status} ({len(bucket)} game(s))")
        if status == "blocked":
            sys.exit("  cookie is stale/blocked -> run refresh_cookie.py, then retry.")
        if status == "empty":
            continue
        parsed = parse_public_matches(html)
        for g in bucket:
            ts, os_ = find_scores(g, parsed)
            if ts is None or os_ is None:
                continue                                   # not played / not found
            if ts == g.get("team_score") and os_ == g.get("opponent_score"):
                continue                                   # no change
            print(f"    {g['team_name']} {g.get('team_score')}-{g.get('opponent_score')}"
                  f" -> {ts}-{os_} vs {g['opponent_name']}")
            patch_score(g["match_id"], g["team_id"], ts, os_)
            notify(g, ts, os_, labels.get(ev))
            changed += 1
        time.sleep(0.4)                                    # be polite to gotSport
    print(f"Done. {changed} score update(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="check all of today's games, ignore the window")
    ap.add_argument("--probe", nargs="+", metavar="EVENT [AGE] [GENDER]",
                    help="no Supabase: fetch+parse one event schedule and print its games")
    ap.add_argument("--harvest-groups", action="store_true",
                    help="map each division to its schedule group id (writes event_groups) and exit")
    args = ap.parse_args()

    if args.probe:
        probe(args.probe[0],
              args.probe[1] if len(args.probe) > 1 else None,
              args.probe[2] if len(args.probe) > 2 else None)
        return
    if args.harvest_groups:
        if not SUPABASE_URL or not SUPABASE_KEY:
            sys.exit("SUPABASE_URL and SUPABASE_KEY must be set in .env.")
        harvest_group_map(load_cookies())
        return
    run(force=args.force)


if __name__ == "__main__":
    main()
