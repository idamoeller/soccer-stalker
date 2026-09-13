#!/usr/bin/env python3
"""
Soccer Stalker poller.

Reads the followed teams from Supabase, pulls each team's matches from the
gotSport JSON API, and upserts them into the `games` table as *team-centric*
rows -- each row is one team's own view of a game (its own score next to the
opponent's), so nothing downstream ever has to reason about home/away.

Runs on GitHub Actions (see .github/workflows/sync.yml), but also runnable
locally:  SUPABASE_URL=... SUPABASE_KEY=... python3 poller.py
Dry run (no Supabase needed, just prints what it would write):
    python3 poller.py --dry-run 781238

Env:
  SUPABASE_URL   e.g. https://xxxx.supabase.co
  SUPABASE_KEY   the Supabase SECRET key (sb_secret_...), which bypasses RLS
"""

import os
import sys
import time
import json
import re
from datetime import datetime, timezone, timedelta
from html import unescape

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GOTSPORT = "https://system.gotsport.com"
ATHLETEONE = "https://api.athleteone.com/api/Script"   # ECNL / TotalGlobalSports

# Look like a real browser hitting the rankings site. From a datacenter IP
# (GitHub Actions / serverless) gotSport bot-challenges bare requests to the
# `upcoming=true` feed; these headers get past that.
GS_HEADERS = {
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://rankings.gotsport.com/",
    "Origin": "https://rankings.gotsport.com",
}

# ECNL's API (athleteone) is origin-locked to theecnl.com.
ECNL_HEADERS = {**GS_HEADERS, "Origin": "https://theecnl.com", "Referer": "https://theecnl.com/", "Accept": "*/*"}
ECNL_LEAGUE = {9: "ECNL", 13: "ECNL RL", 21: "Pre-ECNL"}
MONTHS = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
          "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# One shared HTTP session with automatic retry + backoff. A scheduled run hits
# three external services (Supabase, gotSport, ECNL); a single transient blip on
# any of them -- a connection reset, a timeout, a 5xx, or gotSport's occasional
# bot-challenge from GitHub's datacenter IP -- used to raise and fail the whole
# run (and email a red X). Retrying transient failures a few times with backoff
# makes those self-heal; a genuinely persistent error (e.g. a 4xx auth problem)
# still surfaces via raise_for_status(). urllib3 ships with requests -> no new dep.
from requests.adapters import HTTPAdapter   # noqa: E402
_RETRY_KW = dict(total=4, connect=4, read=4, backoff_factor=1.5,
                 status_forcelist=(429, 500, 502, 503, 504), raise_on_status=False)
try:
    from urllib3.util.retry import Retry
    try:
        _retry = Retry(allowed_methods=frozenset(["GET", "POST"]), **_RETRY_KW)
    except TypeError:                        # older urllib3 spelled it method_whitelist
        _retry = Retry(method_whitelist=frozenset(["GET", "POST"]), **_RETRY_KW)
except Exception:
    _retry = None

HTTP = requests.Session()
if _retry is not None:
    _adapter = HTTPAdapter(max_retries=_retry)
    HTTP.mount("https://", _adapter)
    HTTP.mount("http://", _adapter)

# ---- Smart scheduling -------------------------------------------------------
# The cron fires often, but each run pulls only the teams that actually need it:
#   * a team with a live/unscored game within [-2h, +18h] of kickoff -> polled
#     every run until its score posts, then it drops out (no more hammering);
#   * a newly-followed team with no games yet -> one pull to seed it;
#   * once a day, a full refresh of EVERY team -> discovers new/rescheduled games
#     and mops up any score that posted late (the "once a day after that" net).
# Any other run is a couple of tiny Supabase reads and an early exit -- so on a
# quiet weekday nothing gets polled, and pulls cluster on game days.
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "18"))   # keep chasing an unposted score up to 18h after kickoff
LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "2"))  # warm up before kickoff
REFRESH_HOUR_UTC = int(os.environ.get("REFRESH_HOUR_UTC", "11"))  # daily full refresh (~7am ET): catches later-posted scores + schedule changes, once a day

# ---- Phone alerts (cloud-side) ---------------------------------------------
# The buzzer lives HERE, in the cloud, so alerts fire with the Mac asleep. Dedupe
# state is one column, games.notified_score = the "ts-os" we last pushed for that
# game (seeded once via SQL so existing scores don't blast). NTFY_TOPIC is a
# GitHub Actions secret; if it's unset the push step simply no-ops.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NOTIFY_LOOKBACK_DAYS = int(os.environ.get("NOTIFY_LOOKBACK_DAYS", "3"))
APP_URL = os.environ.get("APP_URL", "https://www.soccerstalker.com")
NOTIFY_FIELDS = ("match_id,team_id,team_name,team_score,opponent_name,"
                 "opponent_score,event_name,notified_score")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pending_score_teams(back_hours, ahead_hours):
    """team_ids that have an UNSCORED game kicking off within [now-back, now+ahead].
    These are the only teams worth polling frequently -- a game that's upcoming,
    live, or recently finished but whose score hasn't posted yet. The instant
    gotSport posts the score (team_score stops being null), that team drops out
    and we stop hammering it. Anything still unscored past the window is left to
    the once-a-day full refresh."""
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(hours=back_hours)).isoformat()
    hi = (now + timedelta(hours=ahead_hours)).isoformat()
    r = HTTP.get(
        f"{SUPABASE_URL}/rest/v1/games", headers=SB_HEADERS,
        params={"select": "team_id",
                "and": f"(match_time.gte.{lo},match_time.lte.{hi},team_score.is.null)"},
        timeout=30,
    )
    r.raise_for_status()
    return {str(row["team_id"]) for row in r.json()}


def teams_missing_games(team_ids):
    """Followed teams that have no games stored yet (just added -> pull now)."""
    if not team_ids:
        return []
    r = HTTP.get(f"{SUPABASE_URL}/rest/v1/games", headers=SB_HEADERS,
                     params={"select": "team_id"}, timeout=30)
    r.raise_for_status()
    have = {str(row["team_id"]) for row in r.json()}
    return [t for t in team_ids if str(t) not in have]


def plan_sync(team_ids):
    """Decide WHICH teams this run pulls. Returns (targets, reason):
      targets is None  -> pull everyone (forced / daily full refresh / safe fallback)
      targets is set() -> pull nobody (skip this run)
      targets is {ids} -> pull just those teams (they have a live/unscored game)."""
    if os.environ.get("FORCE_SYNC") == "1":
        return None, "forced (manual run)"
    now = datetime.now(timezone.utc)
    # Once a day, refresh EVERY team: discovers new/rescheduled games and catches
    # any score that posted late (the "once a day after that" safety net). Only
    # the top-of-hour run inside REFRESH_HOUR_UTC qualifies (cron fires every
    # 10 min; minute<10 keeps it to a single daily refresh).
    if now.hour == REFRESH_HOUR_UTC and now.minute < 10:
        return None, "daily full refresh"
    try:
        follow = {str(t) for t in team_ids}
        missing = set(teams_missing_games(team_ids))          # just-followed -> need a first pull
        pending = pending_score_teams(LOOKBACK_HOURS, LOOKAHEAD_HOURS) & follow
        targets = (missing | pending) & follow
        if targets:
            bits = []
            if pending: bits.append(f"{len(pending)} with a live/unscored game")
            if missing: bits.append(f"{len(missing)} newly-followed")
            return targets, ", ".join(bits)
    except Exception as e:
        # If the check itself fails, don't silently go dark -- pull everyone.
        return None, f"schedule check failed ({e}); pulling all to be safe"
    return set(), "no live or unscored games near now; skipping"


def get_followed_teams():
    """All followed teams across ALL users (secret key bypasses RLS)."""
    r = HTTP.get(
        f"{SUPABASE_URL}/rest/v1/followed_teams",
        headers=SB_HEADERS,
        params={"select": "team_id,name,source,ecnl_org,ecnl_conf,ecnl_club,ecnl_team"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_matches(team_id):
    """Results (Game History) + the real upcoming schedule (Upcoming Games tab),
    de-duped by match id.

    `upcoming=true` is the endpoint the team page's "Upcoming Games" tab uses --
    it includes day-of tournament games that `past=false` misses. Results
    (`past=true`, which carry the authoritative scores) are fetched LAST so a
    completed game's real score always wins over any stale null-score copy."""
    matches = {}
    queries = (
        {"upcoming": "true"},   # future schedule, incl. today's tournament games
        {"past": "false"},      # recently completed / in-between
        {"past": "true"},       # results with scores -- last, so it wins on merge
    )
    for q in queries:
        try:
            r = HTTP.get(
                f"{GOTSPORT}/api/v1/teams/{team_id}/matches",
                params={**q, "page": 1, "per_page": 100},
                headers=GS_HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else data.get("matches", [])
            print(f"      {q}: {len(items)} matches")
            for m in items:
                matches[m["id"]] = m
        except Exception as e:
            print(f"    ! fetch team {team_id} {q} failed: {e}")
    return list(matches.values())


def logo_url(rel):
    if not rel:
        return None
    return GOTSPORT + rel if rel.startswith("/") else rel


def to_row(team_id, m):
    """Turn one gotSport match into this team's own row. The home/away ->
    team mapping happens HERE, once, and is baked into team_score/opponent_*."""
    home = m.get("homeTeam") or {}
    away = m.get("awayTeam") or {}
    try:
        is_home = int(home.get("team_id")) == int(team_id)
    except (TypeError, ValueError):
        is_home = str(away.get("team_id")) != str(team_id)
    me, opp = (home, away) if is_home else (away, home)
    me_score = m.get("home_score") if is_home else m.get("away_score")
    opp_score = m.get("away_score") if is_home else m.get("home_score")
    venue = m.get("venue") or {}
    pitch = m.get("pitch") or {}
    opp_id = opp.get("team_id")
    return {
        "match_id": m["id"],
        "team_id": str(team_id),
        "team_name": me.get("full_name"),
        "team_logo": logo_url(me.get("team_logo")),
        "team_score": me_score,
        "opponent_id": str(opp_id) if opp_id is not None else None,
        "opponent_name": opp.get("full_name"),
        "opponent_logo": logo_url(opp.get("team_logo")),
        "opponent_score": opp_score,
        "is_home": is_home,
        "match_time": m.get("matchTime"),
        "match_date": m.get("match_date"),
        "venue_name": venue.get("name"),
        "venue_address": venue.get("full_address"),
        "field_name": pitch.get("name"),
        "event_id": m.get("event_id"),
        "event_name": m.get("event_name"),
        "division_name": m.get("division_name"),
        "match_number": m.get("match_number"),
        "source": "gotsport",
        "updated_at": now_iso(),
    }


def upsert_games(rows):
    if not rows:
        return
    r = HTTP.post(
        f"{SUPABASE_URL}/rest/v1/games",
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": "match_id,team_id"},
        json=rows,
        timeout=60,
    )
    if not r.ok:
        print(f"  ! upsert failed {r.status_code}: {r.text[:400]}")
        r.raise_for_status()


def _clean_coaches(names):
    """De-dupe + trim coach_names; always return a list (never None) so an
    enriched-but-coachless team ([]) is distinguishable from a pre-migration
    row that was never fetched under the new schema (null)."""
    seen, out = set(), []
    for n in (names or []):
        n = (n or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower()); out.append(n)
    return out


def fetch_club(team_id):
    try:
        r = HTTP.get(f"{GOTSPORT}/api/v1/team_ranking_data/team_details",
                         params={"team_id": team_id}, headers=GS_HEADERS, timeout=20)
        r.raise_for_status()
        d = r.json()
        return {"team_id": str(team_id), "club_name": d.get("club_name"),
                "team_name": d.get("name"),
                "age": d.get("display_age_group"), "gender": d.get("display_gender"),
                "state": d.get("team_association"),
                "coach_names": _clean_coaches(d.get("coach_names")),
                "updated_at": now_iso()}
    except Exception as e:
        print(f"    ! club fetch {team_id} failed: {e}")
        return None


def sync_clubs(rows):
    """Populate the `clubs` cache for every team referenced (followed + opponents),
    fetching team_details only for teams we don't already have."""
    ids = set()
    for r in rows:
        if r.get("team_id"): ids.add(str(r["team_id"]))
        if r.get("opponent_id"): ids.add(str(r["opponent_id"]))
    have = set()
    try:
        resp = HTTP.get(f"{SUPABASE_URL}/rest/v1/clubs", headers=SB_HEADERS,
                            params={"select": "team_id,coach_names"}, timeout=30)
        resp.raise_for_status()
        # A row counts as cached only once it carries the enriched fields
        # (coach_names is [] for a coachless team, null only on pre-migration rows),
        # so older rows get re-fetched once to backfill age/gender/state/coaches.
        have = {row["team_id"] for row in resp.json() if row.get("coach_names") is not None}
    except Exception as e:
        print(f"  ! clubs read failed (table may not exist yet): {e}")
        return
    missing = [i for i in ids if i not in have]
    print(f"Clubs: {len(ids)} referenced, {len(missing)} new to fetch")
    club_rows = []
    for i in missing:
        c = fetch_club(i)
        if c:
            club_rows.append(c)
        time.sleep(0.15)
    for k in range(0, len(club_rows), 200):
        rr = HTTP.post(f"{SUPABASE_URL}/rest/v1/clubs",
                           headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
                           params={"on_conflict": "team_id"}, json=club_rows[k:k + 200], timeout=60)
        if not rr.ok:
            print(f"  ! clubs upsert failed {rr.status_code}: {rr.text[:200]}")


# ---------------- ECNL (TotalGlobalSports / AthleteOne) ----------------
def _ecnl_date(s):
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", s or "")
    return f"{m.group(3)}-{MONTHS.get(m.group(1), '01')}-{int(m.group(2)):02d}" if m else None


def _ecnl_time24(s):
    if not s or s.strip() == "12:00 AM":   # ECNL's "time TBD" placeholder
        return None
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", s or "")
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
    return f"{h:02d}:{m.group(2)}:00"


def _split_venue(v):
    if not v:
        return None, None
    parts = v.rsplit(" - ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (v.strip(), None)


def _ecnl_orient(result, ts, os_):
    """A played game's score is shown from the followed team's perspective
    ("us - them") next to a Win/Loss/Tie icon. The icon self-corrects a row
    that ever renders the other way round (home-away)."""
    if ts is None or os_ is None:
        return ts, os_
    r = result or ""
    if ("win" in r and ts < os_) or ("los" in r and ts > os_):
        return os_, ts
    return ts, os_


def parse_ecnl_games(html, team_id):
    """Parse a team's games from get-individual-team-info HTML (the opponent is
    the `individual-team-item` span; the followed team is implicit)."""
    games = []
    for m in re.finditer(r"<tr>(.*?)</tr>", html, re.S):
        row = m.group(1)
        mid = re.search(r'data-match-id="(\d+)"', row)
        opp = re.search(r'individual-team-item"[^>]*data-club-id="(\d+)"[^>]*data-team-id="(\d+)"[^>]*>([^<]+)</span>', row)
        if not (mid and opp) or opp.group(2) == str(team_id):
            continue
        ha = re.search(r"min-height:\s*63px;[^>]*>\s*([HA])\s*</div>", row)
        date = re.search(r"<div>([A-Z][a-z]{2} \d{1,2}, \d{4})</div>", row)
        tm = re.search(r'padding:\s*5px 0px;">\s*(\d{1,2}:\d{2}\s*[AP]M)', row)
        ven = re.search(r"game-complex-item[^>]*>(.*?)</span>", row, re.S)
        vt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", ven.group(1))).strip() if ven else ""
        # A played game renders a result icon + "us - them" score; an unplayed
        # one renders a "Preview" link and no score.
        sc = re.search(r"/score/(\w+?)_Icon\.png[^>]*/>\s*<span>\s*(\d+)\s*-\s*(\d+)\s*</span>", row)
        games.append({"match_id": mid.group(1), "home": (ha.group(1) == "H") if ha else None,
                      "date": date.group(1) if date else None, "time": tm.group(1) if tm else None,
                      "opp": unescape(opp.group(3)).strip(), "opp_team": opp.group(2),
                      "venue": "" if vt == "-" else vt,
                      "result": sc.group(1).lower() if sc else None,
                      "team_score": int(sc.group(2)) if sc else None,
                      "opp_score": int(sc.group(3)) if sc else None})
    return games


def build_ecnl_rows(t):
    """One followed ECNL team -> team-centric game rows (team_id namespaced 'ecnl-<id>')."""
    org, conf, club, team = t["ecnl_org"], t["ecnl_conf"], t["ecnl_club"], t["ecnl_team"]
    league = ECNL_LEAGUE.get(org, "ECNL")
    tname = t.get("name") or f"Team {team}"
    r = HTTP.get(f"{ATHLETEONE}/get-individual-team-info/{org}/{conf}/{club}/{team}",
                     headers=ECNL_HEADERS, timeout=30)
    r.raise_for_status()
    out = {}
    for g in parse_ecnl_games(r.text, team):
        md = _ecnl_date(g["date"])
        t24 = _ecnl_time24(g["time"]) if g["time"] else None
        vn, fn = _split_venue(g["venue"])
        ts, os_ = _ecnl_orient(g.get("result"), g.get("team_score"), g.get("opp_score"))
        row = {
            "match_id": int(g["match_id"]), "team_id": "ecnl-" + str(team),
            "team_name": tname, "team_logo": None, "team_score": ts,
            "opponent_id": "ecnl-" + g["opp_team"], "opponent_name": g["opp"],
            "opponent_logo": None, "opponent_score": os_, "is_home": g["home"],
            "match_time": (f"{md}T{t24}" if md and t24 else None), "match_date": md,
            "venue_name": vn, "venue_address": None, "field_name": fn,
            "event_id": None, "event_name": league, "division_name": None,
            "match_number": int(g["match_id"]), "source": "ecnl", "updated_at": now_iso(),
        }
        out[(row["match_id"], row["team_id"])] = row
    return list(out.values())


def build_rows(team_ids):
    """Fetch + transform every team's matches into de-duped (match_id, team_id) rows."""
    all_rows = {}
    for tid in team_ids:
        matches = fetch_matches(tid)
        print(f"  team {tid}: {len(matches)} matches")
        for m in matches:
            try:
                row = to_row(tid, m)
                # skip TBD-bracket placeholders (team listed against itself)
                if row.get("opponent_id") and str(row["opponent_id"]) == str(row["team_id"]):
                    continue
                # skip phantom games parked on a far-future sentinel date (e.g. 2035-01-01)
                md = str(row.get("match_date") or "")
                if md[:4].isdigit() and int(md[:4]) >= 2030:
                    continue
                all_rows[(row["match_id"], row["team_id"])] = row
            except Exception as e:
                print(f"    ! row build failed for match {m.get('id')}: {e}")
        time.sleep(0.3)  # be polite to gotSport
    return list(all_rows.values())


# --------------------------------------------------------------------------- #
# Phone alerts: push any game whose score is new/changed since we last notified.
# Reads only the games table, so it fires no matter how the score got there
# (this cloud poller, the Mac, or the Sync button) and with the Mac asleep.
# --------------------------------------------------------------------------- #
def _ntfy(title, message, click):
    """POST one alert to ntfy. Returns True on success. Title must be latin-1
    (ASCII team names are fine; the soccer emoji rides in the Tags header)."""
    headers = {"Title": title, "Tags": "soccer", "Priority": "high"}
    if click:
        headers["Click"] = click
    try:
        r = HTTP.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
        if not r.ok:
            print(f"  ! ntfy {r.status_code}: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"  ! ntfy failed: {e}")
        return False


def _mark_notified(match_id, team_id, score):
    """Record the score we just pushed so we never double-buzz for it."""
    try:
        r = HTTP.patch(f"{SUPABASE_URL}/rest/v1/games",
                       headers={**SB_HEADERS, "Prefer": "return=minimal"},
                       params={"match_id": f"eq.{match_id}", "team_id": f"eq.{team_id}"},
                       json={"notified_score": score}, timeout=30)
        return r.ok
    except requests.RequestException as e:
        print(f"  ! mark-notified failed: {e}")
        return False


def notify_new_scores():
    """Buzz the phone for every followed game whose final score changed since we
    last notified it. Never raises -- a hiccup just retries next run. A missing
    NTFY_TOPIC or notified_score column no-ops cleanly (setup not finished yet)."""
    if not NTFY_TOPIC:
        print("  notify: NTFY_TOPIC not set; skipping push step.")
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NOTIFY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        r = HTTP.get(f"{SUPABASE_URL}/rest/v1/games", headers=SB_HEADERS,
                     params={"select": NOTIFY_FIELDS,
                             "team_score": "not.is.null", "opponent_score": "not.is.null",
                             "match_date": f"gte.{cutoff}"}, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  notify: games read failed ({e}); retrying next run.")
        return
    sent = 0
    for g in rows:
        ts, os_ = g.get("team_score"), g.get("opponent_score")
        if ts is None or os_ is None:
            continue
        cur = f"{ts}-{os_}"
        if g.get("notified_score") == cur:
            continue                                    # already buzzed this exact score
        name, opp = g.get("team_name"), g.get("opponent_name")
        letter = "W" if ts > os_ else "L" if ts < os_ else "T"
        title = f"{name} {ts}-{os_} {opp}"
        where = f" · {g.get('event_name')}" if g.get("event_name") else ""
        message = f"{letter} {ts}-{os_} vs {opp}{where}"
        print(f"  ALERT -> {title}")
        if _ntfy(title, message, APP_URL) and _mark_notified(g["match_id"], g["team_id"], cur):
            sent += 1
    if sent:
        print(f"  notify: {sent} push(es) sent.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv

    if "--dry-run-ecnl" in sys.argv:
        org, conf, club, team = [int(x) for x in args[:4]]
        rows = build_ecnl_rows({"ecnl_org": org, "ecnl_conf": conf, "ecnl_club": club, "ecnl_team": team, "name": "Test ECNL"})
        print(f"[dry run ecnl] {len(rows)} rows:")
        print(json.dumps(rows, indent=2, default=str))
        return

    if dry:
        team_ids = args or ["781238"]
        print(f"[dry run] teams: {team_ids}")
        rows = build_rows(team_ids)
        print(f"[dry run] would upsert {len(rows)} rows:")
        print(json.dumps(rows, indent=2, default=str))
        return

    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_KEY must be set.")

    teams = get_followed_teams()
    gots = list(dict.fromkeys(
        str(t["team_id"]).strip() for t in teams
        if (t.get("source") or "gotsport") != "ecnl" and str(t.get("team_id") or "").strip()))
    ecnl = [t for t in teams if (t.get("source") or "") == "ecnl"]
    print(f"Followed: {len(gots)} gotSport, {len(ecnl)} ECNL")

    # Smart scheduling: pull only the teams that need it right now.
    all_ids = gots + [f"ecnl-{t.get('ecnl_team')}" for t in ecnl]
    targets, reason = plan_sync(all_ids)
    scope = "ALL" if targets is None else ("SKIP" if not targets else f"{len(targets)} team(s)")
    print(f"Sync decision: {scope} -- {reason}")
    if targets is None or targets:                     # ALL, or a specific set of teams
        pull_gots = gots if targets is None else [g for g in gots if g in targets]
        pull_ecnl = ecnl if targets is None else [t for t in ecnl if f"ecnl-{t.get('ecnl_team')}" in targets]
        rows = build_rows(pull_gots)
        for t in pull_ecnl:
            try:
                er = build_ecnl_rows(t)
                print(f"  ECNL team {t.get('ecnl_team')}: {len(er)} games")
                rows += er
            except Exception as e:
                print(f"  ! ECNL team {t.get('ecnl_team')} failed: {e}")
            time.sleep(0.3)
        print(f"Upserting {len(rows)} game rows...")
        for i in range(0, len(rows), 200):
            upsert_games(rows[i:i + 200])
        sync_clubs([r for r in rows if r.get("source") != "ecnl"])
    # Buzz the phone for any new/changed score. Runs on EVERY invocation (even a
    # SKIP) so a score that landed on a prior run still goes out -- and it works
    # with the Mac asleep, which is the whole point.
    notify_new_scores()
    print("Done.")


if __name__ == "__main__":
    main()
