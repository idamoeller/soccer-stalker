// Soccer Stalker — ECNL metadata proxy (Supabase Edge Function)
//
// The browser can't call ECNL's API (origin-locked to theecnl.com, no CORS),
// so this function fetches it server-side (spoofing that origin) and returns
// clean JSON to feed the cascading picker:
//   League -> Conference -> Age -> (Flight, RL only) -> Team
//
// Actions (query param `action`):
//   leagues
//   conferences?season=80
//   ages?org=9&conf=4265
//   flights?org=13&conf=4310&age=22262
//   teams?org=9&season=80&conf=4265&div=40563     (div = age for ECNL, flight for RL)
//
// Deploy from the Supabase dashboard: Edge Functions -> Create -> name it
// "ecnl-meta" -> paste this file -> Deploy.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const A1 = "https://api.athleteone.com/api/Script";
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
  { key: "ecnl-g",    name: "ECNL Girls",     org: 9,  season: 80, hasFlights: false },
  { key: "ecnl-rl-g", name: "ECNL RL Girls",  org: 13, season: 82, hasFlights: true },
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
    if (m) body = m[1];   // isolate the named <select> (needed when a fragment has several)
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
    return json({ error: "unknown action" }, 400);
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});
