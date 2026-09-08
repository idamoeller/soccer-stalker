#!/usr/bin/env python3
"""
Matchday cookie refresher (Mac-native).

gotSport's org_event schedule pages sit behind an *invisible* reCAPTCHA v3
(score-based, not a click-the-traffic-lights puzzle). A real-enough browser
passes it silently; a plain `requests` call or a headless browser gets parked
on the verify page. But once a genuine browser has passed, the session cookie
`_mls_proto_session` lets ordinary Python `requests` fetch ANY event's schedule
page for ~27 hours -- across every tournament and league.

So this script runs a *headed* Chromium once (off-screen, via launchd at ~3am),
lets v3 pass, and saves the resulting gotSport cookies to cookies.json. The
score poller (poll_scores.py) then reuses that cookie all day with no browser.

    .venv/bin/python refresh_cookie.py          # get a fresh cookie now
    .venv/bin/python refresh_cookie.py --show    # print the saved cookie's age

Nothing secret lives in this file. It reads SUPABASE_URL / SUPABASE_KEY from
.env only to pick a currently-relevant event to warm up against (optional).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

GOTSPORT = "https://system.gotsport.com"
COOKIE_FILE = os.path.join(HERE, "cookies.json")
SESSION_COOKIE = "_mls_proto_session"          # the key HttpOnly cookie we need

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Events to warm up against. We prefer events we actually track (pulled from
# Supabase below); these season-long leagues are a stable fallback that should
# exist all year: GA (56497), GA Aspire (56498), GA Inspire (56612), DPL (51974).
FALLBACK_EVENTS = [e for e in os.environ.get("WARMUP_EVENT_IDS", "").split(",") if e.strip()] or \
                  ["56498", "56497", "56612", "51974", "55668", "55944"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Candidate events (best-effort, from the games we actually track)
# --------------------------------------------------------------------------- #
def candidate_events():
    """Distinct gotSport event ids from recently-scheduled games, newest first,
    then the season-league fallbacks. Any valid event page seeds the cookie, so
    this just biases toward events we know exist right now."""
    events = []
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/games",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                params={"select": "event_id", "source": "eq.gotsport",
                        "event_id": "not.is.null", "order": "match_time.desc", "limit": 60},
                timeout=30,
            )
            r.raise_for_status()
            for row in r.json():
                ev = str(row.get("event_id") or "").strip()
                if ev and ev not in events:
                    events.append(ev)
        except Exception as e:
            print(f"  (couldn't read events from Supabase, using fallbacks: {e})")
    for ev in FALLBACK_EVENTS:
        if ev and ev not in events:
            events.append(ev)
    return events[:8]


# --------------------------------------------------------------------------- #
# The browser pass
# --------------------------------------------------------------------------- #
STEALTH = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def harvest_cookies(events):
    """Open a headed (but off-screen) Chromium, visit event schedule pages until
    reCAPTCHA v3 passes and the session cookie is set. Returns a name->value dict
    of gotSport cookies, or None."""
    from playwright.sync_api import sync_playwright

    got = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,                       # headless FAILS v3; headed passes
            args=[
                "--window-position=-3000,-3000",  # off the visible screen
                "--window-size=1200,900",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(user_agent=UA, viewport={"width": 1200, "height": 900},
                                      locale="en-US")
        context.add_init_script(STEALTH)
        page = context.new_page()

        for ev in events:
            url = f"{GOTSPORT}/org_event/events/{ev}/schedules"
            try:
                print(f"  visiting event {ev} ...")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Let v3 score us and any verify->schedule redirect settle.
                for _ in range(6):
                    page.wait_for_timeout(2500)
                    if "verify_captchas" not in page.url:
                        break
                html = page.content()
                cookies = {c["name"]: c["value"] for c in context.cookies()
                           if c["domain"].endswith("gotsport.com")}
                parked = "verify_captchas" in page.url
                have_session = SESSION_COOKIE in cookies
                rendered = "public-match" in html
                print(f"    session={have_session} parked={parked} matches_on_page={rendered}")
                if have_session and not parked:
                    got = cookies
                    if rendered:            # a page that actually rendered games = ideal
                        break
            except Exception as e:
                print(f"    ! {ev}: {e}")

        browser.close()
    return got


def validate(cookies):
    """Confirm the harvested cookie really works from plain requests (no browser).
    A blocked/verify page is tiny (~1KB); a real schedule page is large. We don't
    require rendered games -- the unfiltered /schedules page is a division landing
    and legitimately has none."""
    for ev in candidate_events():
        try:
            r = requests.get(
                f"{GOTSPORT}/org_event/events/{ev}/schedules",
                headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                         "Accept-Language": "en-US,en;q=0.9", "Referer": GOTSPORT + "/"},
                cookies=cookies, timeout=45, allow_redirects=True,
            )
            if "verify_captchas" not in r.url and r.status_code == 200 and len(r.text) > 5000:
                print(f"  validated: requests+cookie fetched event {ev} ({len(r.text)} bytes, not blocked)")
                return True
        except Exception as e:
            print(f"    ! validate {ev}: {e}")
    return False


def save(cookies):
    payload = {"saved_at": now().isoformat(), "cookies": cookies}
    with open(COOKIE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  saved {len(cookies)} cookie(s) -> {COOKIE_FILE}")


def show():
    if not os.path.exists(COOKIE_FILE):
        print("No cookies.json yet. Run without --show to create one.")
        return
    data = json.load(open(COOKIE_FILE))
    saved = datetime.fromisoformat(data["saved_at"])
    age_h = (now() - saved).total_seconds() / 3600
    names = ", ".join(data.get("cookies", {}).keys())
    print(f"cookies.json saved {age_h:.1f}h ago ({saved.isoformat()})")
    print(f"  cookies: {names}")
    print(f"  session cookie present: {SESSION_COOKIE in data.get('cookies', {})}")


def main():
    if "--show" in sys.argv:
        show()
        return

    events = candidate_events()
    print(f"Refreshing gotSport cookie ({now().isoformat()}). Warmup events: {events}")
    cookies = harvest_cookies(events)
    if not cookies or SESSION_COOKIE not in cookies:
        sys.exit("FAILED: browser did not obtain a gotSport session cookie "
                 "(reCAPTCHA v3 may have parked us). Try running again.")

    ok = validate(cookies)
    save(cookies)                     # save even if validation was inconclusive
    if not ok:
        print("  WARNING: could not confirm the cookie works from plain requests; "
              "saved anyway. The poller will report if it's stale.")
    print("Done.")


if __name__ == "__main__":
    main()
