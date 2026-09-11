// Soccer Stalker — ECNL metadata proxy (Supabase Edge Function)
//
// The browser can't call ECNL's API (origin-locked to theecnl.com, no CORS),
// so this function fetches it server-side (spoofing that origin) and returns
// clean JSON to feed the cascading picker:
//   League -> Conference -> Age -> (Flight, RL only) -> Team
//
// Actions (query param `action`):
//   ECNL cascading picker:
//   leagues
//   conferences?season=80
//   ages?org=9&conf=4265
//   flights?org=13&conf=4310&age=22262
//   teams?org=9&season=80&conf=4265&div=40563     (div = age for ECNL, flight for RL)
//   gotSport name search (open ranking API — no auth, requires gender+age):
//   gotsport-search?q=Scorpions&gender=f&age=13&page=1   (optional: state=MA, tier, filter_by)
//   gotSport league picker (server-rendered event pages — no auth, no captcha):
//   gs-event-clubs?event=56498    (club names in a league event, for the picker autocomplete)
//
// Deploy from the Supabase dashboard: Edge Functions -> open the function whose
// slug is "meta-ecnl" -> paste this file (replace all) -> Deploy.
// (The deployed slug is "meta-ecnl"; the app calls /functions/v1/meta-ecnl.
//  This repo folder is named ecnl-meta for historical reasons only.)

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const A1 = "https://api.athleteone.com/api/Script";
// gotSport's public rankings API. Browser-ish headers keep datacenter IPs unblocked
// (same trick the poller uses). Endpoint is open, but wants a real UA/Referer.
const GOTSPORT_API = "https://system.gotsport.com/api/v1";
const GOTSPORT = "https://system.gotsport.com";
const GS_RANK_HEADERS: Record<string, string> = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Accept": "application/json",
  "Referer": "https://rankings.gotsport.com/",
  "Origin": "https://rankings.gotsport.com",
};
const GS_HEADERS: Record<string, string> = {
  "Origin": "https://theecnl.com",
  "Referer": "https://theecnl.com/",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Accept": "*/*",
};
const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

// Leagues we support (each is a distinct org + season in the TGS/AthleteOne system).
const LEAGUES = [
  { key: "ecnl-g",     name: "ECNL Girls",      org: 9,  season: 80, hasFlights: false },
  { key: "ecnl-rl-g",  name: "ECNL RL Girls",   org: 13, season: 82, hasFlights: true },
  // Pre-ECNL Girls has flights (Pre-ECNL I/II) at SOME ages only (e.g. GU12 yes,
  // GU11 no). The front-end checks the flights response per age, so hasFlights=true
  // just means "there may be a flight step" — an empty flights list => skip it.
  { key: "pre-ecnl-g", name: "Pre-ECNL Girls",  org: 21, season: 86, hasFlights: true },
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });
}
function unesc(s: string): string {
  return s.replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">");
}
async function fetchText(path: string): Promise<string> {
  const r = await fetch(`${A1}/${path}`, { headers: GS_HEADERS });
  return await r.text();
}
function parseOptions(html: string, selectId?: string): Array<{ id: string; name: string }> {
  let body = html;
  if (selectId) {
    const m = html.match(new RegExp(`<select[^>]*id="${selectId}"[^>]*>([\\s\\S]*?)</select>`));
    if (!m) return [];    // named <select> absent => no options (e.g. an age with no flights)
    body = m[1];          // isolate the named <select> (a fragment can have several)
  }
  const out: Array<{ id: string; name: string }> = [];
  const seen = new Set<string>();
  for (const m of body.matchAll(/<option[^>]*value="([^"]*)"[^>]*>([^<]*)<\/option>/g)) {
    const id = m[1], name = unesc(m[2]).trim();
    if (id && id !== "0" && name && name !== "--- Select ---" && !seen.has(id)) {
      seen.add(id);
      out.push({ id, name });
    }
  }
  return out;
}
function parseTeams(html: string): Array<{ teamId: string; clubId: string; name: string }> {
  const seen = new Map<string, { teamId: string; clubId: string; name: string }>();
  for (const m of html.matchAll(/<span class="individual-team-item"[^>]*data-club-id="(\d+)"[^>]*data-team-id="(\d+)"[^>]*>([^<]+)<\/span>/g)) {
    const clubId = m[1], teamId = m[2], name = unesc(m[3]).trim();
    if (!seen.has(teamId)) seen.set(teamId, { teamId, clubId, name });
  }
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

// gotSport org_event pages are server-rendered and NOT captcha-gated on the club
// listing — parse the league event's club names to feed the picker's autocomplete.
// (Team following still goes through gotsportSearch, which returns ranking ids.)
function parseEventClubs(html: string, eventId: string): string[] {
  const re = new RegExp(`events/${eventId}/clubs/\\d+"[^>]*>\\s*([^<]+?)\\s*<`, "g");
  const seen = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    const name = unesc(m[1]).replace(/\s+/g, " ").trim();
    if (name.length > 1) seen.add(name);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
}

// Prefix a gotSport relative logo path with the host (matches poller/front-end).
function gsLogo(p: string | null | undefined): string | null {
  if (!p) return null;
  return p.startsWith("http") ? p : `https://system.gotsport.com${p}`;
}

// gotSport ranking search. Requires gender (f|m) + age; team_or_club_name alone
// returns nothing (mirrors the rankings site's own behaviour).
async function gotsportSearch(q: URLSearchParams) {
  const params = new URLSearchParams();
  const put = (k: string, v: string | null) => { if (v) params.append(`search[${k}]`, v); };
  put("team_or_club_name", q.get("q"));
  put("gender", q.get("gender"));          // "f" | "m"
  put("age", q.get("age"));                // integer, e.g. 13 for U13
  put("team_country", q.get("country") || "USA");
  put("team_association", q.get("state")); // 2-letter state code (optional)
  put("tier", q.get("tier"));              // optional
  put("filter_by", q.get("filter_by"));    // optional: national|regional|state
  params.append("search[page]", q.get("page") || "1");

  const r = await fetch(`${GOTSPORT_API}/team_ranking_data?${params.toString()}`, { headers: GS_RANK_HEADERS });
  if (!r.ok) return { error: `gotSport ${r.status}`, teams: [], pagination: null };
  const body = await r.json();
  const teams = (body.team_ranking_data || []).map((t: Record<string, unknown>) => ({
    teamId: String(t.team_id),
    name: t.team_name,
    clubName: t.club_name ?? null,
    gender: t.gender,                       // "f" | "m"
    age: t.age,
    state: t.team_association ?? null,
    logo: gsLogo(t.logo_url_full as string),
    nationalRank: t.national_rank ?? null,
    record: { w: t.total_wins ?? null, l: t.total_losses ?? null, d: t.total_draws ?? null },
  }));
  return { teams, pagination: body.pagination ?? null };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  // Require a signed-in user (picker is used while logged in).
  const url = Deno.env.get("SUPABASE_URL")!;
  const anon = Deno.env.get("SUPABASE_ANON_KEY")!;
  const uc = createClient(url, anon, { global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } } });
  const { data: { user } } = await uc.auth.getUser();
  if (!user) return json({ error: "Not signed in." }, 401);

  const q = new URL(req.url).searchParams;
  const action = q.get("action");
  try {
    if (action === "leagues") return json({ leagues: LEAGUES });
    if (action === "conferences") {
      const season = q.get("season");
      return json({ conferences: parseOptions(await fetchText(`get-event-list-by-season-id/${season}/0`), "event-select") });
    }
    if (action === "ages") {
      const org = q.get("org"), conf = q.get("conf");
      return json({ ages: parseOptions(await fetchText(`get-division-list-by-event-id/${org}/${conf}/0/0`), "schedule-select") });
    }
    if (action === "flights") {
      const org = q.get("org"), conf = q.get("conf"), age = q.get("age");
      return json({ flights: parseOptions(await fetchText(`get-division-list-by-event-id/${org}/${conf}/${age}/${age}`), "flight-select") });
    }
    if (action === "teams") {
      const org = q.get("org"), season = q.get("season"), conf = q.get("conf"), div = q.get("div");
      return json({ teams: parseTeams(await fetchText(`get-conference-schedules/${org}/${season}/${conf}/${div}/0`)) });
    }
    if (action === "gotsport-search") {
      if (!q.get("q")) return json({ error: "Enter a team or club name." }, 400);
      if (!q.get("gender") || !q.get("age")) return json({ error: "Gender and age are required." }, 400);
      return json(await gotsportSearch(q));
    }
    if (action === "gs-event-clubs") {
      const event = q.get("event");
      if (!event || !/^\d+$/.test(event)) return json({ error: "event id required" }, 400);
      const r = await fetch(`${GOTSPORT}/org_event/events/${event}/clubs`, { headers: GS_RANK_HEADERS });
      if (!r.ok) return json({ error: `gotSport ${r.status}`, clubs: [] });
      return json({ clubs: parseEventClubs(await r.text(), event) });
    }
    return json({ error: "unknown action" }, 400);
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});
