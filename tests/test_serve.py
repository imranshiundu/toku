import json
import os
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_toku import NOW, TokuTestCase

import toku


class TestServe(TokuTestCase):
    def setUp(self):
        super().setUp()
        toku.sync(self.source, self.dest, now=NOW)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        super().tearDown()

    def start_server(self):
        self.server = toku.make_dashboard_server("127.0.0.1", 0)
        port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        return port

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.start_server(), path), timeout=10) as resp:
            return resp.status, resp.read()

    def test_dashboard_html(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"toku \xe2\x80\x94 opencode usage", body)

    def test_api_snapshot_fresh_from_db(self):
        with mock.patch.dict(
            os.environ, {"TOKU_SNAPSHOT": os.path.join(self.tmp, "missing.json")}
        ):
            status, body = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        snap = json.loads(body)
        self.assertEqual(snap["windows"]["all"]["requests"], 4)
        self.assertEqual(snap["windows"]["all"]["input_tokens"], 410)

    def test_api_snapshot_reads_file_when_present(self):
        conn = sqlite3.connect(self.dest)
        snap = toku.build_snapshot(conn, now=NOW)
        conn.close()
        toku.write_snapshot(toku.snapshot_path(), snap)
        status, body = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["generated_at"], NOW)

    def test_404(self):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/nope" % self.start_server(), timeout=10
            )
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 404)


if __name__ == "__main__":
    unittest.main()
