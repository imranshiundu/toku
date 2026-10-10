# toku

**Persistent usage tracking for [opencode](https://opencode.ai) — tokens, models, agents, projects, forever.**

opencode stores your sessions in a local SQLite database and **prunes old data** — so your usage history quietly disappears. toku snapshots that usage into its own durable database every 2 hours via cron, so you can always answer: *what did I use in the last 24 hours, week, month, year — ever?*

- **Zero dependencies** — pure Python stdlib, one file
- **<50 MB RAM** (measured: 40.8 MB peak, and it stays flat as history grows — aggregation happens inside SQLite)
- **Never loses history** — upserts never shrink, so data survives opencode's pruning even if toku misses runs
- **Frontend-ready** — writes a `snapshot.json` every sync, and optionally fans out to **Supabase** (REST) or any **PostgreSQL** database

```
toku report — opencode usage, all projects

WINDOW       REQUESTS     INPUT    OUTPUT  REASONING   CACHE-R   CACHE-W     TOTAL       COST
last 24h         1.2K     25.0M    351.3K     232.3K    675.7M         0    701.3M    $0.0000
last 7d          5.7K    180.1M      1.8M       1.1M      2.6B         0      2.8B    $0.0000
last 30d        27.2K      1.8B     10.3M       4.7M      8.0B         0      9.8B    $0.0000
last 365d       27.2K      1.8B     10.3M       4.7M      8.0B         0      9.8B    $0.0000
all-time        27.2K      1.8B     10.3M       4.7M      8.0B         0      9.8B    $0.0000

BY MODEL (all-time)
  opencode/muse-spark-1.3-contributor-free   10.4K req    3.9B tok  40.4% ########
  ...
BY PROJECT (all-time)
  taptap-api                                  6.6K req    3.1B tok  31.7% ######
  ...
```

## Install

Anyone can install it — or just ask your coding agent to do it for you:

```bash
curl -fsSL https://raw.githubusercontent.com/imranshiundu/toku/main/install.sh | bash
```

Or, if you cloned this repo:

```bash
bash install.sh
```

Or, in your agent (opencode, Claude Code, Cursor, …):

> Install toku from https://github.com/imranshiundu/toku — it's a usage tracker for opencode. Follow its README.

The installer:

1. copies `toku.py` to `~/.local/share/toku/`
2. installs a cron entry that runs `toku.py sync` **every 2 hours** (idempotent, marker `# toku:cron`)
3. runs the first sync and prints your report

Data lives in `~/.local/share/toku/`:

| File | What |
|---|---|
| `toku.db` | durable hourly usage aggregates (SQLite) |
| `snapshot.json` | machine-readable windows — 24h/7d/30d/365d/all-time with by-model/by-project/by-agent breakdowns |
| `toku.log` | cron output |

Check any time:

```bash
python3 ~/.local/share/toku/toku.py report
```

## How it works

toku reads opencode's own database (`~/.local/share/opencode/opencode.db`, auto-detected — never writes to it) and aggregates every `step-finish` part — **one per model call** — into hourly rows:

```
(hour_utc, project_id, model_id, agent, requests, input_tokens, output_tokens,
 reasoning_tokens, cache_read_tokens, cache_write_tokens, cost)
```

Each sync recomputes aggregates from whatever opencode still has and upserts with a **never-shrink** rule (`MAX(old, new)`): if opencode prunes data, toku's history keeps the last known numbers. Windows are rolling and hour-aligned ("last 24h" = the last 24 hour-buckets).

**Correctness receipt:** toku's all-time sums match opencode's own per-session token rollups exactly, on a real database with 27.2K requests (input 1,762,133,213 / output 10,330,954 / cache-read 8,004,207,444 / reasoning 4,745,103 — identical on both sides).

## Frontend / remote database (optional)

Point a `config` file at a database your frontend can read — toku pushes the full aggregate set on every sync (idempotent upserts, so it's safe to miss runs):

```bash
mkdir -p ~/.local/share/toku
cat > ~/.local/share/toku/config <<EOF
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=your-service-or-anon-key
EOF
```

Or any PostgreSQL (needs `pip install 'psycopg[binary]'`):

```
DATABASE_URL=postgresql://user:pass@host:5432/db
```

Run the DDL in [`sql/remote.sql`](sql/remote.sql) on the server once. You get two tables: `toku_usage` (hourly aggregates) and `toku_projects` (id → label). Supabase needs no Python dependencies at all — it pushes over REST.

For a local frontend, just read `~/.local/share/toku/snapshot.json` — every window pre-computed, no database needed.

## Uninstall

```bash
bash uninstall.sh           # removes cron, keeps data
bash uninstall.sh --purge   # removes cron + all data
```

## Development

```bash
make verify    # lint (py_compile, bash -n, shellcheck if present) + 12 unit tests
```

Tests build a fixture opencode database and verify aggregation, idempotency, the never-shrink guarantee, windows, labels and the RAM budget (<50 MB, asserted on Linux).

## License

MIT — see [LICENSE](LICENSE).
