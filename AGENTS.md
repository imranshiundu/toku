# AGENTS.md

toku is a persistent usage tracker for opencode: it snapshots token/model/agent/project usage from opencode's local SQLite database into its own durable database every 2 hours via cron, because opencode prunes old data and history disappears.

**Hard rule: never modify the user's desktop environment.** No shell extensions, no theme changes, no WM restarts, no gsettings edits. toku runs as a plain cron job and CLI — that boundary is deliberate. (A GNOME top-bar extension was attempted once, the owner's shell misbehaved during removal of an unrelated handover, and the owner ended the experiment. Don't repeat it.)

## When asked to install toku on a user's machine

1. Clone or download this repo, then run `bash install.sh` from the repo root (or the one-liner in README).
2. `install.sh` copies `toku.py` to `~/.local/share/toku/`, installs an idempotent cron entry (every 2h, marker `# toku:cron`), runs the first sync and prints a report.
3. Verify: `python3 ~/.local/share/toku/toku.py report` shows windows (24h/7d/30d/365d/all) with nonzero data if opencode has been used.
4. Optional remote: put `SUPABASE_URL`/`SUPABASE_KEY` (or `DATABASE_URL` for Postgres) in `~/.local/share/toku/config`, and run the DDL in `sql/remote.sql` on the server first.

## When changing code

- `toku.py` is the main backend: stdlib only — do not add runtime dependencies (the 50 MB RAM budget and zero-install promise depend on it).
- `tiny/main.go` is a full Go port (sync/report/snapshot/Supabase) for no-Python machines and tiny ARM boards. It MUST stay behaviorally identical to `toku.py` — `tests/test_tiny.py` enforces row-level parity; build with `make tiny` (needs a Go toolchain) so the parity tests run.
- Verification: `make verify` (or `python3 -m unittest discover -s tests`). All tests must pass; no suppressed failures.
- Never break the never-shrink guarantee in the upsert: local history must survive source pruning. There are tests for this — keep them passing.
- Update `CHANGELOG.md` and burn items from `DEBT_LEDGER.md` in the same change that resolves them.
