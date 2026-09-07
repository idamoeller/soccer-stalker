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
    source: "gotsport",
    updated_at: new Date().toISOString(),
  };
}

// ---------------- ECNL (TotalGlobalSports / AthleteOne) ----------------
const ECNL = "https://api.athleteone.com/api/Script";
const ECNL_HEADERS: Record<string, string> = { ...GS_HEADERS, "Origin": "https://theecnl.com", "Referer": "https://theecnl.com/", "Accept": "*/*" };
const ECNL_LEAGUE: Record<number, string> = { 9: "ECNL", 13: "ECNL RL", 21: "Pre-ECNL" };
const MONTHS: Record<string, string> = { Jan: "01", Feb: "02", Mar: "03", Apr: "04", May: "05", Jun: "06", Jul: "07", Aug: "08", Sep: "09", Oct: "10", Nov: "11", Dec: "12" };
function unesc(s: string): string { return s.replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">"); }
function ecnlDate(s: string): string | null { const m = s.match(/([A-Z][a-z]{2}) (\d{1,2}), (\d{4})/); return m ? `${m[3]}-${MONTHS[m[1]] || "01"}-${String(m[2]).padStart(2, "0")}` : null; }
function ecnlTime24(s: string): string | null { if (!s || s.trim() === "12:00 AM") return null; const m = s.match(/(\d{1,2}):(\d{2})\s*([AP]M)/); if (!m) return null; let h = Number(m[1]) % 12; if (m[3] === "PM") h += 12; return `${String(h).padStart(2, "0")}:${m[2]}:00`; }
function splitVenue(v: string | null): [string | null, string | null] { if (!v) return [null, null]; const i = v.lastIndexOf(" - "); return i >= 0 ? [v.slice(0, i).trim(), v.slice(i + 3).trim()] : [v.trim(), null]; }
function parseEcnlGames(html: string, teamId: string): any[] {
  const games: any[] = [];
  for (const m of html.matchAll(/<tr>([\s\S]*?)<\/tr>/g)) {
    const row = m[1];
    const mid = row.match(/data-match-id="(\d+)"/);
    const opp = row.match(/individual-team-item"[^>]*data-club-id="(\d+)"[^>]*data-team-id="(\d+)"[^>]*>([^<]+)<\/span>/);
    if (!mid || !opp || opp[2] === String(teamId)) continue;
    const ha = row.match(/min-height:\s*63px;[^>]*>\s*([HA])\s*<\/div>/);
    const date = row.match(/<div>([A-Z][a-z]{2} \d{1,2}, \d{4})<\/div>/);
    const tm = row.match(/padding:\s*5px 0px;">\s*(\d{1,2}:\d{2}\s*[AP]M)/);
    const ven = row.match(/game-complex-item[^>]*>([\s\S]*?)<\/span>/);
    let vt = ven ? ven[1].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim() : "";
    if (vt === "-") vt = "";
    games.push({ matchId: mid[1], home: ha ? ha[1] === "H" : null, date: date ? date[1] : null, time: tm ? tm[1] : null, opp: unesc(opp[3]).trim(), oppTeam: opp[2], venue: vt });
  }
  return games;
}
async function buildEcnlRows(t: any): Promise<any[]> {
  const org = t.ecnl_org, conf = t.ecnl_conf, club = t.ecnl_club, team = t.ecnl_team;
  const league = ECNL_LEAGUE[org] || "ECNL";
  const r = await fetch(`${ECNL}/get-individual-team-info/${org}/${conf}/${club}/${team}`, { headers: ECNL_HEADERS });
  const html = await r.text();
  const out = new Map<string, any>();
  for (const g of parseEcnlGames(html, String(team))) {
    const md = g.date ? ecnlDate(g.date) : null;
    const t24 = g.time ? ecnlTime24(g.time) : null;
    const [vn, fn] = splitVenue(g.venue);
    const row = {
      match_id: Number(g.matchId), team_id: "ecnl-" + team, team_name: t.name || ("Team " + team),
      team_logo: null, team_score: null, opponent_id: "ecnl-" + g.oppTeam, opponent_name: g.opp,
      opponent_logo: null, opponent_score: null, is_home: g.home,
      match_time: (md && t24) ? `${md}T${t24}` : null, match_date: md,
      venue_name: vn, venue_address: null, field_name: fn,
      event_id: null, event_name: league, division_name: null, match_number: Number(g.matchId),
      source: "ecnl", updated_at: new Date().toISOString(),
    };
    out.set(row.match_id + ":" + row.team_id, row);
  }
  return [...out.values()];
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
  const { data: teams, error: teamsErr } = await admin.from("followed_teams")
    .select("team_id,name,source,ecnl_org,ecnl_conf,ecnl_club,ecnl_team");
  if (teamsErr) return json({ error: teamsErr.message }, 500);

  const gots = [...new Set((teams ?? []).filter((t: any) => (t.source || "gotsport") !== "ecnl").map((t: any) => String(t.team_id)).filter(Boolean))];
  const ecnlTeams = (teams ?? []).filter((t: any) => t.source === "ecnl");
  const rows = new Map<string, any>();
  for (const tid of gots) {
    for (const m of await fetchMatches(tid)) {
      const row = toRow(tid, m);
      // skip TBD-bracket placeholders (team listed against itself)
      if (row.opponent_id && row.opponent_id === row.team_id) continue;
      rows.set(`${row.match_id}:${row.team_id}`, row);
    }
  }
  for (const t of ecnlTeams) {
    try { for (const row of await buildEcnlRows(t)) rows.set(`${row.match_id}:${row.team_id}`, row); }
    catch (_e) { /* skip a failing ECNL team */ }
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
    // clubs cache is gotSport-only (numeric ids); skip namespaced 'ecnl-...' ids
    for (const r of all) {
      if (r.team_id && /^\d+$/.test(String(r.team_id))) refIds.add(String(r.team_id));
      if (r.opponent_id && /^\d+$/.test(String(r.opponent_id))) refIds.add(String(r.opponent_id));
    }
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

  return json({ ok: true, teams: gots.length, ecnlTeams: ecnlTeams.length, games: all.length, newClubs });
});
