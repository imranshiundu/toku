# Changelog

All notable changes to toku are documented here.

## [0.1.1] — 2026-10-11

### Added

- **`tiny/` — full Go port** for no-Python machines and tiny ARM boards: `sync`, `report`, `snapshot.json`, Supabase push — same schema, same never-shrink upsert, verified row-identical against the Python implementation (parity test in the suite, runs when the binary is built via `make tiny`). Static binaries: amd64, arm64 (Pi Zero 2/3/4/5), armv6 (Pi Zero 1/W). Measured 11.3 MB peak RSS on small databases (Python: 22.3 MB); ~42 MB worst-case on a 14.8 GB source — both under the 50 MB budget.
- **`serve` command (Python)** — zero-config live dashboard over HTTP: `python3 toku.py serve --host 0.0.0.0` and open `http://<lan-ip>:7480` on your phone or any browser; auto-refreshes every 60s. Also serves `/api/snapshot` (fresh JSON, CORS-enabled) for frontends.
- `AGENTS.md` hard rule recorded: toku never touches the user's desktop environment.

## [0.1.0] — 2026-10-10

- Initial release.
- `toku.py sync` — snapshots usage from opencode's local SQLite database into toku's own durable database: hourly rows per (project, model, agent) with requests, input/output/reasoning/cache tokens and cost. Upserts never shrink, so history survives opencode's data pruning.
- `toku.py report` — usage windows: last 24h / 7d / 30d / 365d / all-time, plus all-time breakdowns by model, project and agent.
- `snapshot.json` written on every sync: machine-readable windows for any frontend.
- Remote fan-out (optional): Supabase via REST (zero dependencies) or any PostgreSQL via `DATABASE_URL` (optional `psycopg`).
- `install.sh` / `uninstall.sh` — idempotent cron setup (every 2 hours by default), curl-able one-liner.
- stdlib only, measured peak RSS well under the 50 MB budget.
