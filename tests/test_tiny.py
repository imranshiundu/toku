import json
import os
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_toku import NOW, TokuTestCase, expected_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TINY_BIN = os.environ.get("TOKU_TINY_BIN") or os.path.join(
    REPO_ROOT, "tiny", "dist", "toku-linux-amd64"
)


@unittest.skipUnless(
    os.path.exists(TINY_BIN) and os.access(TINY_BIN, os.X_OK),
    "tiny binary not built (run: make tiny)",
)
class TestTinyParity(TokuTestCase):
    def setUp(self):
        super().setUp()
        self.dest2 = os.path.join(self.tmp, "tiny.db")
        self.snap2 = os.path.join(self.tmp, "tiny-snapshot.json")

    def run_tiny(self):
        return subprocess.run(
            [TINY_BIN, "sync", "--source", self.source, "--db", self.dest2, "--snapshot", self.snap2],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def tiny_rows(self):
        conn = sqlite3.connect(self.dest2)
        rows = conn.execute(
            "SELECT hour_utc, project_id, model_id, agent, requests, input_tokens, "
            "output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, cost "
            "FROM toku_usage ORDER BY hour_utc, project_id, model_id"
        ).fetchall()
        conn.close()
        return rows

    def test_aggregates_match_python(self):
        out = self.run_tiny()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.tiny_rows(), expected_rows())

    def test_idempotent(self):
        self.run_tiny()
        first = self.tiny_rows()
        self.run_tiny()
        self.assertEqual(first, self.tiny_rows())

    def test_prune_never_shrinks_local_history(self):
        self.run_tiny()
        before = self.tiny_rows()
        conn = sqlite3.connect(self.source)
        conn.execute("DELETE FROM part WHERE id = 'pt3'")
        conn.commit()
        conn.close()
        self.run_tiny()
        self.assertEqual(before, self.tiny_rows())

    def test_snapshot_json(self):
        out = self.run_tiny()
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.snap2) as fh:
            snap = json.load(fh)
        self.assertEqual(snap["windows"]["all"]["requests"], 4)
        self.assertEqual(snap["windows"]["all"]["input_tokens"], 410)
        self.assertEqual(
            {m["id"] for m in snap["windows"]["all"]["by_model"]},
            {"prov/model-a", "prov/model-b"},
        )
        self.assertEqual(snap["version"], "0.1.1")


if __name__ == "__main__":
    unittest.main()
