-- Score protection: never let a NULL score overwrite a real one.
--
-- Two writers touch the `games` table: the GitHub-Actions poller (reads the
-- per-team feed, which can LAG up to 24h and briefly report a played game as
-- still null) and the Matchday scraper (reads the bracket page, which is fresh).
-- Without this, a lagging feed run could wipe a freshly-scraped score back to
-- null. This trigger keeps the old value whenever an update would null out a
-- score that was already set. Fresh, non-null scores always win.
--
-- Run once in the Supabase SQL editor.

create or replace function protect_scores()
returns trigger as $$
begin
  if new.team_score is null and old.team_score is not null then
    new.team_score := old.team_score;
  end if;
  if new.opponent_score is null and old.opponent_score is not null then
    new.opponent_score := old.opponent_score;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_protect_scores on games;

create trigger trg_protect_scores
  before update on games
  for each row
  execute function protect_scores();
