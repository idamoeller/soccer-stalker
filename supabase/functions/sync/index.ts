// Soccer Stalker — on-demand sync (Supabase Edge Function)
//
// Same job as poller.py, but callable from the website's "↻ Sync" button.
// The button invokes this with the signed-in user's JWT; we verify the user,
// then pull every followed team's matches from gotSport and upsert into `games`
// using the service-role key (which bypasses RLS).
//
// Env vars are injected automatically by Supabase: SUPABASE_URL,
// SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY.
//
// Deploy from the Supabase dashboard: Edge Functions → Create function →
// name it "sync" → paste this file → Deploy.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const GOTSPORT = "https://system.gotsport.com";

// Look like a browser so gotSport doesn't bot-challenge the upcoming feed.
const GS_HEADERS: Record<string, string> = {
  "Accept": "application/json",
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Referer": "https://rankings.gotsport.com/",
  "Origin": "https://rankings.gotsport.com",
};

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

function logoUrl(rel: string | null | undefined): string | null {
  if (!rel) return null;
  return rel.startsWith("/") ? GOTSPORT + rel : rel;
}

function toRow(teamId: string, m: any): any {
  const home = m.homeTeam || {};
  const away = m.awayTeam || {};
  let isHome: boolean;
  const ht = Number(home.team_id), aw = Number(away.team_id), tid = Number(teamId);
  if (!Number.isNaN(ht) && ht === tid) isHome = true;
  else if (!Number.isNaN(aw) && aw === tid) isHome = false;
  else isHome = String(home.team_id) === String(teamId);
  const me = isHome ? home : away;
  const opp = isHome ? away : home;
  const meScore = isHome ? m.home_score : m.away_score;
  const oppScore = isHome ? m.away_score : m.home_score;
  const venue = m.venue || {};
  const pitch = m.pitch || {};
  return {
    match_id: m.id,
    team_id: String(teamId),
    team_name: me.full_name ?? null,
    team_logo: logoUrl(me.team_logo),
    team_score: meScore ?? null,
    opponent_id: opp.team_id != null ? String(opp.team_id) : null,
    opponent_name: opp.full_name ?? null,
    opponent_logo: logoUrl(opp.team_logo),
    opponent_score: oppScore ?? null,
    is_home: isHome,
    match_time: m.matchTime ?? null,
    match_date: m.match_date ?? null,
    venue_name: venue.name ?? null,
    venue_address: venue.full_address ?? null,
    field_name: pitch.name ?? null,
    event_id: m.event_id ?? null,
    event_name: m.event_name ?? null,
    division_name: m.division_name ?? null,
    match_number: m.match_number ?? null,
    updated_at: new Date().toISOString(),
  };
}

async function fetchMatches(teamId: string): Promise<any[]> {
  const merged = new Map<number, any>();
  // upcoming first, results (past=true) last so real scores win on merge
  const queries = [{ upcoming: "true" }, { past: "false" }, { past: "true" }];
  for (const q of queries) {
    try {
      const p = new URLSearchParams({ ...q, page: "1", per_page: "100" } as Record<string, string>);
      const r = await fetch(`${GOTSPORT}/api/v1/teams/${teamId}/matches?${p}`, { headers: GS_HEADERS });
      const data = await r.json();
      const items = Array.isArray(data) ? data : (data.matches ?? []);
      for (const m of items) merged.set(m.id, m);
    } catch (_e) { /* skip this query */ }
  }
  return [...merged.values()];
}

async function fetchClub(teamId: string): Promise<any | null> {
  try {
    const r = await fetch(`${GOTSPORT}/api/v1/team_ranking_data/team_details?team_id=${teamId}`, { headers: GS_HEADERS });
    const d = await r.json();
    return { team_id: String(teamId), club_name: d.club_name ?? null, team_name: d.name ?? null, updated_at: new Date().toISOString() };
  } catch (_e) { return null; }
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  const url = Deno.env.get("SUPABASE_URL")!;
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

  // Require a signed-in user (the button sends their JWT).
  const authHeader = req.headers.get("Authorization") ?? "";
  const userClient = createClient(url, anonKey, { global: { headers: { Authorization: authHeader } } });
  const { data: { user } } = await userClient.auth.getUser();
  if (!user) return json({ error: "Not signed in." }, 401);

  const admin = createClient(url, serviceKey);
  const { data: teams, error: teamsErr } = await admin.from("followed_teams").select("team_id");
  if (teamsErr) return json({ error: teamsErr.message }, 500);

  const ids = [...new Set((teams ?? []).map((t: any) => String(t.team_id)))];
  const rows = new Map<string, any>();
  for (const tid of ids) {
    for (const m of await fetchMatches(tid)) {
      const row = toRow(tid, m);
      // skip TBD-bracket placeholders (team listed against itself)
      if (row.opponent_id && row.opponent_id === row.team_id) continue;
      rows.set(`${row.match_id}:${row.team_id}`, row);
    }
  }

  const all = [...rows.values()];
  for (let i = 0; i < all.length; i += 200) {
    const { error } = await admin.from("games").upsert(all.slice(i, i + 200), { onConflict: "match_id,team_id" });
    if (error) return json({ error: error.message, upserted: i }, 500);
  }

  // Fill the clubs cache for any team (followed + opponents) we don't have yet.
  let newClubs = 0;
  try {
    const refIds = new Set<string>();
    for (const r of all) { if (r.team_id) refIds.add(String(r.team_id)); if (r.opponent_id) refIds.add(String(r.opponent_id)); }
    const { data: known } = await admin.from("clubs").select("team_id");
    const have = new Set((known ?? []).map((c: any) => String(c.team_id)));
    const missing = [...refIds].filter((i) => !have.has(i));
    const clubRows: any[] = [];
    for (const i of missing) { const c = await fetchClub(i); if (c) clubRows.push(c); }
    for (let i = 0; i < clubRows.length; i += 200) {
      await admin.from("clubs").upsert(clubRows.slice(i, i + 200), { onConflict: "team_id" });
    }
    newClubs = clubRows.length;
  } catch (_e) { /* clubs table may not exist yet */ }

  return json({ ok: true, teams: ids.length, games: all.length, newClubs });
});
