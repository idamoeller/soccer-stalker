-- Soccer Stalker clock: Supabase pg_cron starts the GitHub "Sync scores"
-- workflow (poller.py), because GitHub's own cron is best-effort and can skip
-- hours at a time (it did on game day, 2026-10-03).
--
-- Run once in the Supabase SQL editor. Safe to re-run: cron.schedule updates a
-- job that already has the same name. Needs a Vault secret named
-- github_dispatch_token = a fine-grained GitHub token limited to
-- idamoeller/soccer-stalker with "Actions: Read and write".
--
-- Check it's working:
--   select status_code, content, error_msg, created
--   from net._http_response order by created desc limit 5;
-- 204 = GitHub accepted the dispatch. 401 = bad/expired token.
-- 403/404 = the token can't see this repo or lacks Actions write.

-- 1) The scheduler (pg_cron) and outbound web requests (pg_net).
create extension if not exists pg_cron with schema pg_catalog;
grant usage on schema cron to postgres;
grant all privileges on all tables in schema cron to postgres;
create extension if not exists pg_net with schema extensions;

-- 2) Every 10 minutes: a normal run (poller.plan_sync pulls only the teams
--    with a game near now, then sends any new-score alerts).
select cron.schedule(
  'soccer-sync-every-10-min',
  '*/10 * * * *',
  $$
  select net.http_post(
    url := 'https://api.github.com/repos/idamoeller/soccer-stalker/actions/workflows/sync.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
      'Accept', 'application/vnd.github+json',
      'Content-Type', 'application/json',
      'User-Agent', 'soccer-stalker-clock',
      'X-GitHub-Api-Version', '2022-11-28'
    ),
    body := '{"ref": "main", "inputs": {"full": "false"}}'::jsonb,
    timeout_milliseconds := 10000
  );
  $$
);

-- 3) Once a day at 11:00 UTC (~7am Eastern): a full refresh of every team,
--    so new or rescheduled games and late-posted scores get picked up.
select cron.schedule(
  'soccer-sync-daily-full',
  '0 11 * * *',
  $$
  select net.http_post(
    url := 'https://api.github.com/repos/idamoeller/soccer-stalker/actions/workflows/sync.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
      'Accept', 'application/vnd.github+json',
      'Content-Type', 'application/json',
      'User-Agent', 'soccer-stalker-clock',
      'X-GitHub-Api-Version', '2022-11-28'
    ),
    body := '{"ref": "main", "inputs": {"full": "true"}}'::jsonb,
    timeout_milliseconds := 10000
  );
  $$
);

-- 4) Show the scheduled jobs (expect both, active = true).
select jobid, jobname, schedule, active from cron.job order by jobid;
