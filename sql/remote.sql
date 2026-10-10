-- toku remote schema — runs on Supabase (SQL editor) or any PostgreSQL.
-- The sync job upserts every 2 hours; nothing here writes on its own.

create table if not exists public.toku_usage (
  hour_utc           bigint not null,
  project_id         text   not null,
  model_id           text   not null,
  agent              text   not null default 'unknown',
  requests           bigint not null default 0,
  input_tokens       bigint not null default 0,
  output_tokens      bigint not null default 0,
  reasoning_tokens   bigint not null default 0,
  cache_read_tokens  bigint not null default 0,
  cache_write_tokens bigint not null default 0,
  cost               double precision not null default 0,
  updated_at         bigint not null default 0,
  primary key (hour_utc, project_id, model_id, agent)
);

create table if not exists public.toku_projects (
  project_id text primary key,
  label      text not null,
  worktree   text
);

-- Supabase: use the service_role key for pushes (it bypasses RLS).
-- If you use the anon key instead, keep RLS enabled and add policies:
-- alter table public.toku_usage enable row level security;
-- create policy "anon insert-update" on public.toku_usage
--   for all to anon using (true) with check (true);
