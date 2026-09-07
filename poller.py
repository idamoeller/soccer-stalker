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
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GOTSPORT = "https://system.gotsport.com"

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

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_followed_team_ids():
    """Distinct team_ids across ALL users (the poller uses the secret key,
    which bypasses RLS, so it sees every followed team)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/followed_teams",
        headers=SB_HEADERS,
        params={"select": "team_id"},
        timeout=30,
    )
    r.raise_for_status()
    ids = []
    for row in r.json():
        tid = str(row["team_id"]).strip()
        if tid and tid not in ids:
            ids.append(tid)
    return ids


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

    if dry:
        team_ids = args or ["781238"]
        print(f"[dry run] teams: {team_ids}")
        rows = build_rows(team_ids)
        print(f"[dry run] would upsert {len(rows)} rows:")
        print(json.dumps(rows, indent=2, default=str))
        return

    if not SUPABASE_URL or not SUPABASE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_KEY must be set.")

    team_ids = get_followed_team_ids()
    print(f"Followed teams: {len(team_ids)} -> {team_ids}")
    rows = build_rows(team_ids)
    print(f"Upserting {len(rows)} game rows...")
    for i in range(0, len(rows), 200):
        upsert_games(rows[i:i + 200])
    print("Done.")


if __name__ == "__main__":
    main()
