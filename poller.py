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

# ---- Smart scheduling -------------------------------------------------------
# The cron fires often, but a real gotSport pull only happens when it's worth
# it: around a known kickoff, once a day to refresh the schedule, or when a
# newly-followed team has no games yet. Any other run is a couple of tiny
# Supabase reads and an early exit -- so on a quiet weekday nothing gets polled,
# and the pulls naturally cluster on game days (usually weekends).
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "5"))    # games run long + score-entry lag
LOOKAHEAD_HOURS = int(os.environ.get("LOOKAHEAD_HOURS", "2"))  # warm up before kickoff
REFRESH_HOUR_UTC = int(os.environ.get("REFRESH_HOUR_UTC", "11"))  # daily schedule refresh (~7am ET)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def any_game_in_window(back_hours, ahead_hours):
    """True if any followed game kicks off within [now-back, now+ahead].
    Every games row is a followed team's game, so one hit means it's game time."""
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(hours=back_hours)).isoformat()
    hi = (now + timedelta(hours=ahead_hours)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/games", headers=SB_HEADERS,
        params={"select": "match_id", "and": f"(match_time.gte.{lo},match_time.lte.{hi})", "limit": 1},
        timeout=30,
    )
    r.raise_for_status()
    return len(r.json()) > 0


def teams_missing_games(team_ids):
    """Followed teams that have no games stored yet (just added -> pull now)."""
    if not team_ids:
        return []
    r = requests.get(f"{SUPABASE_URL}/rest/v1/games", headers=SB_HEADERS,
                     params={"select": "team_id"}, timeout=30)
    r.raise_for_status()
    have = {str(row["team_id"]) for row in r.json()}
    return [t for t in team_ids if str(t) not in have]


def should_sync(team_ids):
    """Decide whether this run does a real pull. Returns (bool, reason)."""
    if os.environ.get("FORCE_SYNC") == "1":
        return True, "forced (manual run)"
    now = datetime.now(timezone.utc)
    # Once a day, refresh the schedule so new/rescheduled games get picked up.
    # Only the top-of-hour run inside REFRESH_HOUR_UTC qualifies (cron fires
    # every 10 min; minute<10 keeps it to a single daily refresh).
    if now.hour == REFRESH_HOUR_UTC and now.minute < 10:
        return True, "daily schedule refresh"
    try:
        missing = teams_missing_games(team_ids)
        if missing:
            return True, f"{len(missing)} newly-followed team(s) with no games yet"
        if any_game_in_window(LOOKBACK_HOURS, LOOKAHEAD_HOURS):
            return True, "game in progress / imminent"
    except Exception as e:
        # If the schedule check itself fails, don't silently go dark -- sync.
        return True, f"schedule check failed ({e}); syncing to be safe"
    return False, "no game near now; skipping pull"


def get_followed_teams():
    """All followed teams across ALL users (secret key bypasses RLS)."""
    r = requests.get(
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
            r = requests.get(
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
    r = requests.post(
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
        r = requests.get(f"{GOTSPORT}/api/v1/team_ranking_data/team_details",
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
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/clubs", headers=SB_HEADERS,
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
        rr = requests.post(f"{SUPABASE_URL}/rest/v1/clubs",
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
        games.append({"match_id": mid.group(1), "home": (ha.group(1) == "H") if ha else None,
                      "date": date.group(1) if date else None, "time": tm.group(1) if tm else None,
                      "opp": unescape(opp.group(3)).strip(), "opp_team": opp.group(2),
                      "venue": "" if vt == "-" else vt})
    return games


def build_ecnl_rows(t):
    """One followed ECNL team -> team-centric game rows (team_id namespaced 'ecnl-<id>')."""
    org, conf, club, team = t["ecnl_org"], t["ecnl_conf"], t["ecnl_club"], t["ecnl_team"]
    league = ECNL_LEAGUE.get(org, "ECNL")
    tname = t.get("name") or f"Team {team}"
    r = requests.get(f"{ATHLETEONE}/get-individual-team-info/{org}/{conf}/{club}/{team}",
                     headers=ECNL_HEADERS, timeout=30)
    r.raise_for_status()
    out = {}
    for g in parse_ecnl_games(r.text, team):
        md = _ecnl_date(g["date"])
        t24 = _ecnl_time24(g["time"]) if g["time"] else None
        vn, fn = _split_venue(g["venue"])
        row = {
            "match_id": int(g["match_id"]), "team_id": "ecnl-" + str(team),
            "team_name": tname, "team_logo": None, "team_score": None,
            "opponent_id": "ecnl-" + g["opp_team"], "opponent_name": g["opp"],
            "opponent_logo": None, "opponent_score": None, "is_home": g["home"],
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
                all_rows[(row["match_id"], row["team_id"])] = row
            except Exception as e:
                print(f"    ! row build failed for match {m.get('id')}: {e}")
        time.sleep(0.3)  # be polite to gotSport
    return list(all_rows.values())


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

    # Smart scheduling: only do the real pull when there's something to pull for.
    all_ids = gots + [f"ecnl-{t.get('ecnl_team')}" for t in ecnl]
    go, reason = should_sync(all_ids)
    print(f"Sync decision: {'RUN' if go else 'SKIP'} -- {reason}")
    if not go:
        return
    rows = build_rows(gots)
    for t in ecnl:
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
    print("Done.")


if __name__ == "__main__":
    main()
