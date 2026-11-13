"""Log auto delete: what is deleted, persistence and the web API."""

import base64
import http.client
import json
import os
import tempfile
import time
import unittest

from estyma_local import retention as R
from estyma_local.web import start_web

DAY = 86400


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = time.time()

    def touch(self, name, age_days):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("{}\n")
        t = self.now - age_days * DAY
        os.utime(path, (t, t))
        return path

    def make(self, env=None):
        return R.LogRetention(self.dir, environ=env or {}, clock=lambda: self.now)

    def test_off_by_default_keeps_everything(self):
        self.touch("exchanges-2020-01-01.jsonl", 400)
        r = self.make()
        self.assertFalse(r.enabled)
        self.assertEqual(r.days, R.DEFAULT_DAYS)
        self.assertEqual(r.sweep(), [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "exchanges-2020-01-01.jsonl")))

    def test_deletes_only_old_jsonl(self):
        for name in ("exchanges-old.jsonl", "cloud-old.jsonl", "writes-old.jsonl"):
            self.touch(name, 10)
        self.touch("exchanges-new.jsonl", 2)
        self.touch("state.json", 100)
        self.touch("registers.json", 100)
        self.touch(".hidden.jsonl", 100)
        os.mkdir(os.path.join(self.dir, "dir.jsonl"))
        r = self.make({"LOG_RETENTION_DAYS": "7"})
        self.assertTrue(r.enabled)
        self.assertEqual(sorted(r.sweep()), ["cloud-old.jsonl", "exchanges-old.jsonl", "writes-old.jsonl"])
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, [".hidden.jsonl", "dir.jsonl", "exchanges-new.jsonl",
                                "registers.json", "state.json"])
        self.assertEqual(r.status()["deleted_total"], 3)

    def test_configure_persists_and_wins_over_env(self):
        r = self.make({"LOG_RETENTION_DAYS": "7"})
        r.configure(False, 14)
        again = self.make({"LOG_RETENTION_DAYS": "7"})
        self.assertEqual((again.enabled, again.days), (False, 14))
        again.configure(True, 3)
        self.assertEqual((self.make().enabled, self.make().days), (True, 3))

    def test_configure_validates_days(self):
        r = self.make()
        for bad in (0, 3651, 1.5, "7", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                r.configure(True, bad)
        self.assertFalse(r.enabled)

    def test_bad_env_is_off(self):
        self.assertFalse(self.make({"LOG_RETENTION_DAYS": "soon"}).enabled)

    def test_background_sweep_after_change(self):
        self.touch("exchanges-old.jsonl", 10)
        r = self.make()
        r.start()
        r.configure(True, 5)
        end = time.time() + 5
        while os.path.exists(os.path.join(self.dir, "exchanges-old.jsonl")) and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "exchanges-old.jsonl")))


class RetentionApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ret = R.LogRetention(self.dir, environ={})
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()

    def ui(self, auth):
        user, pw = auth or (None, None)
        srv = start_web("127.0.0.1", 0, self.dir, user, pw, retention=self.ret)
        self.servers.append(srv)
        return srv.server_address[1]

    def call(self, port, method, body=None, auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        conn.request(method, "/api/log-retention",
                     body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data) if r.getheader("Content-Type") == "application/json" else None

    def test_change_needs_ui_auth(self):
        port = self.ui(None)
        code, st = self.call(port, "GET")
        self.assertEqual(code, 200)
        self.assertFalse(st["enabled"])
        self.assertFalse(st["ui_auth"])
        code, _ = self.call(port, "POST", {"enabled": True, "days": 7})
        self.assertEqual(code, 403)
        self.assertFalse(self.ret.enabled)

    def test_change_with_auth(self):
        port = self.ui(("u", "p"))
        code, _ = self.call(port, "POST", {"enabled": True, "days": 7})
        self.assertEqual(code, 401)
        code, res = self.call(port, "POST", {"enabled": True, "days": 0}, auth="u:p")
        self.assertEqual(code, 400)
        code, res = self.call(port, "POST", {"enabled": "yes", "days": 7}, auth="u:p")
        self.assertEqual(code, 400)
        code, res = self.call(port, "POST", {"enabled": True, "days": 7}, auth="u:p")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"] and res["enabled"])
        self.assertEqual(res["days"], 7)
        self.assertEqual((self.ret.enabled, self.ret.days), (True, 7))

    def test_missing_without_retention(self):
        srv = start_web("127.0.0.1", 0, self.dir)
        self.servers.append(srv)
        code, _ = self.call(srv.server_address[1], "GET")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
