# Changelog

All notable changes to toku are documented here.

## [0.1.0] — 2026-10-10

- Initial release.
- `toku.py sync` — snapshots usage from opencode's local SQLite database into toku's own durable database: hourly rows per (project, model, agent) with requests, input/output/reasoning/cache tokens and cost. Upserts never shrink, so history survives opencode's data pruning.
- `toku.py report` — usage windows: last 24h / 7d / 30d / 365d / all-time, plus all-time breakdowns by model, project and agent.
- `snapshot.json` written on every sync: machine-readable windows for any frontend.
- Remote fan-out (optional): Supabase via REST (zero dependencies) or any PostgreSQL via `DATABASE_URL` (optional `psycopg`).
- `install.sh` / `uninstall.sh` — idempotent cron setup (every 2 hours by default), curl-able one-liner.
- stdlib only, measured peak RSS well under the 50 MB budget.
