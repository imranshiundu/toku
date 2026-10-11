import io
import json
import os
import resource
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toku

OPENCODE_SCHEMA = """
CREATE TABLE `message` (
  `id` text PRIMARY KEY,
  `session_id` text NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL,
  `data` text NOT NULL
);
CREATE TABLE `part` (
  `id` text PRIMARY KEY,
  `message_id` text NOT NULL,
  `session_id` text NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL,
  `data` text NOT NULL
);
CREATE TABLE `session` (
  `id` text PRIMARY KEY,
  `project_id` text NOT NULL,
  `directory` text NOT NULL,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL
);
CREATE TABLE `project` (
  `id` text PRIMARY KEY,
  `worktree` text NOT NULL,
  `name` text
);
"""

NOW = 1_790_000_000


def hour_floor(ts):
    return (ts // 3600) * 3600


def step_finish(tokens, cost=0):
    return json.dumps({"type": "step-finish", "tokens": tokens, "cost": cost, "reason": "stop"})


def make_source(path, now=NOW):
    conn = sqlite3.connect(path)
    conn.executescript(OPENCODE_SCHEMA)
    conn.execute("INSERT INTO project VALUES ('p1', '/home/x/proj-a', NULL)")
    conn.execute("INSERT INTO project VALUES ('named', '/home/x/other', 'Custom Name')")
    conn.execute("INSERT INTO project VALUES ('global', '/', NULL)")
    conn.executemany(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        [
            ("s1", "p1", "/home/x/proj-a", now, now),
            ("s2", "global", "/home/imran/Downloads", now, now),
        ],
    )

    def message(mid, sid, data, ts):
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, sid, ts, ts, data))

    def part(pid, mid, sid, data, ts):
        conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)", (pid, mid, sid, ts, ts, data))

    msg_a = json.dumps(
        {
            "role": "assistant",
            "agent": "build",
            "modelID": "model-a",
            "providerID": "prov",
            "cost": 0,
            "time": {"created": now},
        }
    )
    msg_b = json.dumps(
        {
            "role": "assistant",
            "agent": "plan",
            "modelID": "model-b",
            "providerID": "prov",
            "cost": 0,
            "time": {"created": now},
        }
    )
    msg_user = json.dumps({"role": "user", "time": {"created": now}})

    message("m1", "s1", msg_a, (now - 7200) * 1000)
    message("m2", "s2", msg_b, (now - 7200) * 1000)
    message("m3", "s1", msg_a, (now - 30 * 3600) * 1000)
    message("m_user", "s1", msg_user, now * 1000)

    t_a = {"total": 160, "input": 100, "output": 10, "reasoning": 5, "cache": {"write": 0, "read": 50}}
    t_a2 = {"total": 60, "input": 60, "output": 0, "reasoning": 0, "cache": {"write": 0, "read": 0}}
    t_b = {"total": 307, "input": 200, "output": 20, "reasoning": 0, "cache": {"write": 7, "read": 80}}
    t_old = {"total": 55, "input": 50, "output": 5, "reasoning": 0, "cache": {"write": 0, "read": 0}}

    part("pt1", "m1", "s1", step_finish(t_a), (now - 7200) * 1000)
    part("pt1b", "m1", "s1", step_finish(t_a2), (now - 7200 + 60) * 1000)
    part("pt2", "m2", "s2", step_finish(t_b, cost=0.01), (now - 7200) * 1000)
    part("pt3", "m3", "s1", step_finish(t_old), (now - 30 * 3600) * 1000)
    part("pt_tool", "m1", "s1", json.dumps({"type": "tool", "state": {}}), (now - 7200) * 1000)
    conn.commit()
    conn.close()
    return path


def expected_rows(now=NOW):
    return [
        (hour_floor(now - 30 * 3600), "p1", "prov/model-a", "build", 1, 50, 5, 0, 0, 0, 0.0),
        (hour_floor(now - 7200), "global", "prov/model-b", "plan", 1, 200, 20, 0, 80, 7, 0.01),
        (hour_floor(now - 7200), "p1", "prov/model-a", "build", 2, 160, 10, 5, 50, 0, 0.0),
    ]


class TokuTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="toku-test-")
        self.source = make_source(os.path.join(self.tmp, "opencode.db"))
        self.dest = os.path.join(self.tmp, "toku.db")
        env = {
            "TOKU_DATA_DIR": self.tmp,
            "TOKU_DB": self.dest,
            "TOKU_SNAPSHOT": os.path.join(self.tmp, "snapshot.json"),
        }
        self.env_patch = mock.patch.dict(os.environ, env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def rows(self):
        conn = sqlite3.connect(self.dest)
        rows = conn.execute(
            "SELECT hour_utc, project_id, model_id, agent, requests, input_tokens, "
            "output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, cost "
            "FROM toku_usage ORDER BY hour_utc, project_id, model_id"
        ).fetchall()
        conn.close()
        return rows


class TestSync(TokuTestCase):
    def test_aggregates_step_finish_parts(self):
        toku.sync(self.source, self.dest, now=NOW)
        self.assertEqual(self.rows(), expected_rows())

    def test_idempotent(self):
        toku.sync(self.source, self.dest, now=NOW)
        first = self.rows()
        toku.sync(self.source, self.dest, now=NOW)
        self.assertEqual(first, self.rows())

    def test_prune_never_shrinks_local_history(self):
        toku.sync(self.source, self.dest, now=NOW)
        before = self.rows()
        conn = sqlite3.connect(self.source)
        conn.execute("DELETE FROM part WHERE id = 'pt3'")
        conn.commit()
        conn.close()
        toku.sync(self.source, self.dest, now=NOW)
        self.assertEqual(before, self.rows())

    def test_projects_labels(self):
        toku.sync(self.source, self.dest, now=NOW)
        conn = sqlite3.connect(self.dest)
        labels = dict(conn.execute("SELECT project_id, label FROM toku_projects").fetchall())
        conn.close()
        self.assertEqual(
            labels, {"p1": "proj-a", "named": "Custom Name", "global": "global"}
        )

    def test_meta_written(self):
        toku.sync(self.source, self.dest, now=NOW)
        conn = sqlite3.connect(self.dest)
        meta = dict(conn.execute("SELECT key, value FROM toku_meta").fetchall())
        conn.close()
        self.assertEqual(meta["last_sync"], str(NOW))
        self.assertEqual(meta["version"], toku.VERSION)

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            toku.sync(os.path.join(self.tmp, "nope.db"), self.dest)


class TestSnapshot(TokuTestCase):
    def test_windows(self):
        toku.sync(self.source, self.dest, now=NOW)
        conn = sqlite3.connect(self.dest)
        snap = toku.build_snapshot(conn, now=NOW)
        conn.close()
        w24 = snap["windows"]["24h"]
        wall = snap["windows"]["all"]
        self.assertEqual(w24["requests"], 3)
        self.assertEqual(w24["input_tokens"], 100 + 60 + 200)
        self.assertEqual(w24["total_tokens"], 165 + 60 + 307)
        self.assertEqual(wall["requests"], 4)
        self.assertEqual(wall["input_tokens"], 100 + 60 + 200 + 50)
        self.assertEqual(
            [m["id"] for m in w24["by_model"]],
            ["prov/model-b", "prov/model-a"],
        )
        self.assertEqual(w24["by_project"][0]["label"], "global")
        self.assertEqual(
            {p["label"] for p in w24["by_project"]}, {"global", "proj-a"}
        )
        self.assertEqual(snap["generated_at"], NOW)

    def test_snapshot_file_written(self):
        toku.sync(self.source, self.dest, now=NOW)
        out = io.StringIO()
        env = {"TOKU_SNAPSHOT": os.path.join(self.tmp, "snapshot.json")}
        with mock.patch.dict(os.environ, env):
            conn = sqlite3.connect(self.dest)
            snap = toku.build_snapshot(conn, now=NOW)
            toku.write_snapshot(toku.snapshot_path(), snap)
            conn.close()
        with open(os.path.join(self.tmp, "snapshot.json")) as fh:
            loaded = json.load(fh)
        self.assertEqual(loaded["windows"]["all"]["requests"], 4)


class TestConfig(TokuTestCase):
    def test_config_file_and_env_override(self):
        os.makedirs(os.path.dirname(toku.config_path()), exist_ok=True)
        with open(toku.config_path(), "w") as fh:
            fh.write("# comment\nSOURCE_DB=/from-file\nSUPABASE_URL=https://x.supabase.co\nBADLINE\n")
        cfg = toku.load_config()
        self.assertEqual(cfg["SOURCE_DB"], "/from-file")
        self.assertEqual(cfg["SUPABASE_URL"], "https://x.supabase.co")
        with mock.patch.dict(os.environ, {"TOKU_SOURCE_DB": "/from-env"}):
            cfg = toku.load_config()
        self.assertEqual(cfg["SOURCE_DB"], "/from-env")

    def test_remote_target(self):
        self.assertIsNone(toku.remote_target({}))
        self.assertEqual(
            toku.remote_target({"SUPABASE_URL": "u", "SUPABASE_KEY": "k"}), "supabase"
        )
        self.assertEqual(toku.remote_target({"DATABASE_URL": "pg://"}), "postgres")


class TestReport(TokuTestCase):
    def test_report_output(self):
        toku.sync(self.source, self.dest, now=NOW)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TOKU_DB": self.dest}):
            with redirect_stdout(out):
                rc = toku.cmd_report(argparse_stub())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("last 24h", text)
        self.assertIn("all", text)
        self.assertIn("prov/model-a", text)
        self.assertIn("proj-a", text)
        self.assertIn("BY AGENT", text)


class argparse_stub:
    def __init__(self):
        self.source = None


class TestMemory(TokuTestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "ru_maxrss units differ per platform")
    def test_sync_peak_rss_under_50mb(self):
        toku.sync(self.source, self.dest, now=NOW)
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertLess(ru, 50 * 1024, "peak RSS %.1f MB exceeds 50 MB budget" % (ru / 1024))


if __name__ == "__main__":
    unittest.main()
