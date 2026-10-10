#!/usr/bin/env python3
"""toku — persistent usage tracking for opencode.

opencode stores sessions in a local SQLite database and prunes old data,
so usage history disappears over time. toku snapshots that usage into its
own durable database — hourly, per model / agent / project — so you can
always answer: what did I use in the last 24 hours, week, month, year, ever?

stdlib only, <50MB RAM, designed to run from cron every 2 hours.

usage:
  python3 toku.py sync      aggregate usage + write snapshot.json (+ optional remote push)
  python3 toku.py report    print usage windows (24h / 7d / 30d / 365d / all-time)

config file (~/.local/share/toku/config), KEY=VALUE lines:
  SOURCE_DB=/path/to/opencode.db
  SUPABASE_URL=https://xxxx.supabase.co
  SUPABASE_KEY=service_or_anon_key
  DATABASE_URL=postgresql://...

environment overrides:
  TOKU_DATA_DIR, TOKU_DB, TOKU_SNAPSHOT, TOKU_SOURCE_DB,
  TOKU_SUPABASE_URL, TOKU_SUPABASE_KEY, TOKU_DATABASE_URL
"""

import argparse
import json
import os
import resource
import sqlite3
import sys
import time
import urllib.request

VERSION = "0.1.0"

WINDOWS = (
    ("24h", 24 * 3600),
    ("7d", 7 * 24 * 3600),
    ("30d", 30 * 24 * 3600),
    ("365d", 365 * 24 * 3600),
    ("all", None),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS toku_usage (
  hour_utc INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT 'unknown',
  requests INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (hour_utc, project_id, model_id, agent)
);
CREATE TABLE IF NOT EXISTS toku_projects (
  project_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  worktree TEXT
);
CREATE TABLE IF NOT EXISTS toku_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

AGG_SQL = """
INSERT INTO toku_usage
  (hour_utc, project_id, model_id, agent, requests,
   input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens,
   cost, updated_at)
SELECT
  (src.part.time_created / 3600000) * 3600,
  src.session.project_id,
  COALESCE(json_extract(src.message.data, '$.providerID'), 'unknown') || '/' ||
    COALESCE(json_extract(src.message.data, '$.modelID'), 'unknown'),
  COALESCE(json_extract(src.message.data, '$.agent'), 'unknown'),
  COUNT(*),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.input')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.output')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.reasoning')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.cache.read')), 0),
  COALESCE(SUM(json_extract(src.part.data, '$.tokens.cache.write')), 0),
  COALESCE(SUM(CAST(json_extract(src.part.data, '$.cost') AS REAL)), 0.0),
  ?
FROM src.part
JOIN src.message ON src.message.id = src.part.message_id
JOIN src.session ON src.session.id = src.part.session_id
WHERE json_extract(src.part.data, '$.type') = 'step-finish'
GROUP BY 1, 2, 3, 4
ON CONFLICT (hour_utc, project_id, model_id, agent) DO UPDATE SET
  requests = MAX(requests, excluded.requests),
  input_tokens = MAX(input_tokens, excluded.input_tokens),
  output_tokens = MAX(output_tokens, excluded.output_tokens),
  reasoning_tokens = MAX(reasoning_tokens, excluded.reasoning_tokens),
  cache_read_tokens = MAX(cache_read_tokens, excluded.cache_read_tokens),
  cache_write_tokens = MAX(cache_write_tokens, excluded.cache_write_tokens),
  cost = MAX(cost, excluded.cost),
  updated_at = excluded.updated_at
"""

TOTALS_COLS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost",
)


def data_dir():
    base = (
        os.environ.get("TOKU_DATA_DIR")
        or os.environ.get("XDG_DATA_HOME")
        or os.path.expanduser("~/.local/share")
    )
    return os.path.join(base, "toku")


def toku_db_path():
    return os.environ.get("TOKU_DB") or os.path.join(data_dir(), "toku.db")


def snapshot_path():
    return os.environ.get("TOKU_SNAPSHOT") or os.path.join(data_dir(), "snapshot.json")


def config_path():
    return os.path.join(data_dir(), "config")


def load_config():
    cfg = {}
    try:
        with open(config_path(), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    env_overrides = {
        "SOURCE_DB": os.environ.get("TOKU_SOURCE_DB"),
        "SUPABASE_URL": os.environ.get("TOKU_SUPABASE_URL"),
        "SUPABASE_KEY": os.environ.get("TOKU_SUPABASE_KEY"),
        "DATABASE_URL": os.environ.get("TOKU_DATABASE_URL"),
    }
    for key, value in env_overrides.items():
        if value:
            cfg[key] = value
    return cfg


def resolve_source_db(cfg):
    explicit = cfg.get("SOURCE_DB")
    if explicit:
        return explicit
    default = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if os.path.exists(default):
        return default
    import subprocess

    try:
        out = subprocess.run(
            ["opencode", "db", "path"],
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
        if out.returncode == 0:
            for line in reversed(out.stdout.strip().splitlines()):
                if line and os.path.exists(line):
                    return line
    except Exception:
        pass
    return default


def open_dest(dest):
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(dest, uri=True)
    conn.executescript(SCHEMA)
    return conn


def attach_source(conn, source):
    try:
        conn.execute("ATTACH DATABASE ? AS src", ("file:%s?mode=ro" % source,))
    except sqlite3.Error:
        conn.execute("ATTACH DATABASE ? AS src", (source,))


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO toku_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def project_label(project_id, name, worktree):
    name = (name or "").strip()
    if name:
        return name
    if worktree and worktree.strip("/"):
        return os.path.basename(worktree.rstrip("/")) or project_id
    return project_id


def refresh_projects(conn):
    rows = conn.execute("SELECT id, name, worktree FROM src.project").fetchall()
    for pid, name, worktree in rows:
        conn.execute(
            "INSERT OR REPLACE INTO toku_projects (project_id, label, worktree) VALUES (?, ?, ?)",
            (pid, project_label(pid, name, worktree), worktree),
        )
    return len(rows)


def sync(source, dest, now=None):
    if not os.path.exists(source):
        raise FileNotFoundError(source)
    now = int(now if now is not None else time.time())
    conn = open_dest(dest)
    attach_source(conn, source)
    try:
        cur = conn.execute(AGG_SQL, (now,))
        rows_upserted = max(cur.rowcount, 0)
        projects = refresh_projects(conn)
        set_meta(conn, "last_sync", now)
        set_meta(conn, "source_db", source)
        set_meta(conn, "version", VERSION)
        conn.commit()
        rows_total = conn.execute("SELECT COUNT(*) FROM toku_usage").fetchone()[0]
    finally:
        conn.close()
    return {
        "source": source,
        "dest": dest,
        "rows_upserted": rows_upserted,
        "projects": projects,
        "rows_total": rows_total,
        "db_bytes": os.path.getsize(dest) if os.path.exists(dest) else 0,
    }


def window_totals(conn, since):
    where = "WHERE hour_utc >= ?" if since is not None else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(requests), 0), "
        "COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
        "COALESCE(SUM(reasoning_tokens), 0), COALESCE(SUM(cache_read_tokens), 0), "
        "COALESCE(SUM(cache_write_tokens), 0), COALESCE(SUM(cost), 0.0) "
        "FROM toku_usage " + where,
        () if since is None else (since,),
    ).fetchone()
    totals = dict(zip(TOTALS_COLS, row))
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["reasoning_tokens"]
        + totals["cache_read_tokens"]
        + totals["cache_write_tokens"]
    )
    return totals


def breakdown(conn, dimension, since, limit=15):
    if dimension == "project":
        sql = (
            "SELECT u.project_id, COALESCE(p.label, u.project_id), "
            "SUM(u.requests), SUM(u.input_tokens), SUM(u.output_tokens), SUM(u.reasoning_tokens), "
            "SUM(u.cache_read_tokens), SUM(u.cache_write_tokens), "
            "SUM(u.input_tokens + u.output_tokens + u.reasoning_tokens + u.cache_read_tokens + u.cache_write_tokens) "
            "FROM toku_usage u LEFT JOIN toku_projects p ON p.project_id = u.project_id "
        )
        params = ()
        if since is not None:
            sql += "WHERE u.hour_utc >= ? "
            params = (since,)
        sql += "GROUP BY u.project_id, 2 ORDER BY 9 DESC, 1 LIMIT ?"
    else:
        sql = (
            "SELECT %s, %s, SUM(requests), SUM(input_tokens), SUM(output_tokens), SUM(reasoning_tokens), "
            "SUM(cache_read_tokens), SUM(cache_write_tokens), "
            "SUM(input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_write_tokens) "
            "FROM toku_usage " % (dimension, dimension)
        )
        params = ()
        if since is not None:
            sql += "WHERE hour_utc >= ? "
            params = (since,)
        sql += "GROUP BY 1, 2 ORDER BY 9 DESC, 1 LIMIT ?"
    out = []
    for row in conn.execute(sql, params + (limit,)):
        out.append(
            {
                "id": row[0],
                "label": row[1],
                "requests": row[2],
                "input_tokens": row[3],
                "output_tokens": row[4],
                "reasoning_tokens": row[5],
                "cache_read_tokens": row[6],
                "cache_write_tokens": row[7],
                "total_tokens": row[8],
            }
        )
    return out


def build_snapshot(conn, now=None):
    now = int(now if now is not None else time.time())
    snap = {"version": VERSION, "generated_at": now, "windows": {}}
    for name, secs in WINDOWS:
        since = now - secs if secs else None
        window = {"since": since}
        window.update(window_totals(conn, since))
        window["by_model"] = breakdown(conn, "model_id", since)
        window["by_project"] = breakdown(conn, "project", since)
        window["by_agent"] = breakdown(conn, "agent", since)
        snap["windows"][name] = window
    return snap


def write_snapshot(path, snap):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_usage_rows(conn):
    return conn.execute(
        "SELECT hour_utc, project_id, model_id, agent, requests, input_tokens, "
        "output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, "
        "cost, updated_at FROM toku_usage"
    ).fetchall()


def fetch_project_rows(conn):
    return conn.execute("SELECT project_id, label, worktree FROM toku_projects").fetchall()


def remote_target(cfg):
    if cfg.get("SUPABASE_URL") and cfg.get("SUPABASE_KEY"):
        return "supabase"
    if cfg.get("DATABASE_URL"):
        return "postgres"
    return None


def push_supabase(cfg, conn):
    url = cfg["SUPABASE_URL"].rstrip("/")
    key = cfg["SUPABASE_KEY"]
    usage = fetch_usage_rows(conn)
    projects = fetch_project_rows(conn)
    usage_keys = (
        "hour_utc",
        "project_id",
        "model_id",
        "agent",
        "requests",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost",
        "updated_at",
    )

    def post(table, payload, on_conflict):
        req = urllib.request.Request(
            "%s/rest/v1/%s?on_conflict=%s" % (url, table, on_conflict),
            data=json.dumps(payload).encode(),
            headers={
                "apikey": key,
                "Authorization": "Bearer %s" % key,
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()

    for chunk in chunks([dict(zip(("project_id", "label", "worktree"), r)) for r in projects], 500):
        post("toku_projects", chunk, "project_id")
    for chunk in chunks([dict(zip(usage_keys, r)) for r in usage], 500):
        post("toku_usage", chunk, "hour_utc,project_id,model_id,agent")
    return len(usage), len(projects)


def push_postgres(cfg, conn):
    try:
        import psycopg
    except ImportError:
        raise RuntimeError(
            "DATABASE_URL is set but psycopg is not installed. "
            "Run: pip install 'psycopg[binary]'"
        )
    usage = fetch_usage_rows(conn)
    projects = fetch_project_rows(conn)
    sql = (
        "INSERT INTO toku_usage (hour_utc, project_id, model_id, agent, requests, "
        "input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, "
        "cost, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (hour_utc, project_id, model_id, agent) DO UPDATE SET "
        "requests = EXCLUDED.requests, input_tokens = EXCLUDED.input_tokens, "
        "output_tokens = EXCLUDED.output_tokens, reasoning_tokens = EXCLUDED.reasoning_tokens, "
        "cache_read_tokens = EXCLUDED.cache_read_tokens, cache_write_tokens = EXCLUDED.cache_write_tokens, "
        "cost = EXCLUDED.cost, updated_at = EXCLUDED.updated_at"
    )
    with psycopg.connect(cfg["DATABASE_URL"]) as pg:
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO toku_projects (project_id, label, worktree) VALUES (%s, %s, %s) "
                "ON CONFLICT (project_id) DO UPDATE SET label = EXCLUDED.label, "
                "worktree = EXCLUDED.worktree",
                projects,
            )
            cur.executemany(sql, usage)
        pg.commit()
    return len(usage), len(projects)


def peak_rss_mb():
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return ru / 1048576
    return ru / 1024


def cmd_sync(args):
    cfg = load_config()
    source = args.source or resolve_source_db(cfg)
    dest = toku_db_path()
    try:
        stats = sync(source, dest)
    except FileNotFoundError:
        print(
            "toku: no opencode database found at %s — nothing to sync. "
            "Run opencode once, or set SOURCE_DB in %s" % (source, config_path())
        )
        return 0
    conn = sqlite3.connect(dest)
    try:
        snap = build_snapshot(conn)
        write_snapshot(snapshot_path(), snap)
        target = remote_target(cfg)
        remote_status = "not configured (local only)"
        if target == "supabase":
            usage_n, proj_n = push_supabase(cfg, conn)
            remote_status = "supabase: %d usage rows, %d projects pushed" % (usage_n, proj_n)
        elif target == "postgres":
            usage_n, proj_n = push_postgres(cfg, conn)
            remote_status = "postgres: %d usage rows, %d projects pushed" % (usage_n, proj_n)
    finally:
        conn.close()
    print(
        "toku sync: %d rows upserted, %d rows total, %.1f KB db — from %s"
        % (stats["rows_upserted"], stats["rows_total"], stats["db_bytes"] / 1024, source)
    )
    print("  snapshot: %s" % snapshot_path())
    print("  remote:   %s" % remote_status)
    print("  peak_rss: %.1f MB" % peak_rss_mb())
    return 0


def human(n):
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return str(int(n))


def money(cost):
    return "$%.4f" % cost if cost < 0.01 else "$%.2f" % cost


def print_breakdown(title, rows, total_tokens):
    print("\n%s" % title)
    if not rows:
        print("  (no data)")
        return
    for row in rows:
        share = (row["total_tokens"] / total_tokens * 100) if total_tokens else 0.0
        bar = "#" * int(round(share / 5))
        print(
            "  %-44s %9s req  %11s tok  %5.1f%% %s"
            % (row["label"][:44], human(row["requests"]), human(row["total_tokens"]), share, bar)
        )


def cmd_report(args):
    dest = toku_db_path()
    if not os.path.exists(dest):
        print("toku: no data yet — run 'python3 toku.py sync' first")
        return 1
    conn = sqlite3.connect(dest)
    try:
        now = int(time.time())
        print("toku report — opencode usage, all projects")
        print("generated: %s" % time.strftime("%Y-%m-%d %H:%M %Z"))
        header = (
            "%-10s %10s %9s %9s %10s %9s %9s %9s %10s"
            % ("WINDOW", "REQUESTS", "INPUT", "OUTPUT", "REASONING", "CACHE-R", "CACHE-W", "TOTAL", "COST")
        )
        print("\n" + header)
        all_totals = None
        for name, secs in WINDOWS:
            since = now - secs if secs else None
            t = window_totals(conn, since)
            if name == "all":
                all_totals = t
            print(
                "%-10s %10s %9s %9s %10s %9s %9s %9s %10s"
                % (
                    "all-time" if name == "all" else "last " + name,
                    human(t["requests"]),
                    human(t["input_tokens"]),
                    human(t["output_tokens"]),
                    human(t["reasoning_tokens"]),
                    human(t["cache_read_tokens"]),
                    human(t["cache_write_tokens"]),
                    human(t["total_tokens"]),
                    money(t["cost"]),
                )
            )
        print_breakdown(
            "BY MODEL (all-time)",
            breakdown(conn, "model_id", None, limit=10),
            all_totals["total_tokens"],
        )
        print_breakdown(
            "BY PROJECT (all-time)",
            breakdown(conn, "project", None, limit=10),
            all_totals["total_tokens"],
        )
        print_breakdown(
            "BY AGENT (all-time)",
            breakdown(conn, "agent", None, limit=10),
            all_totals["total_tokens"],
        )
    finally:
        conn.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="toku", description="persistent usage tracking for opencode"
    )
    parser.add_argument("--version", action="version", version="toku %s" % VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    p_sync = sub.add_parser("sync", help="aggregate usage + write snapshot (+ optional remote push)")
    p_sync.add_argument("--source", help="path to the opencode database (default: auto-detect)")
    sub.add_parser("report", help="print usage windows (24h / 7d / 30d / 365d / all-time)")
    args = parser.parse_args(argv)
    if args.command == "sync":
        return cmd_sync(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
