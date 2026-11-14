"""The cloud timeout fallback setting: defaults, persistence, web API."""

import base64
import http.client
import json
import os
import tempfile
import unittest

from estyma_local import cloud_fallback as F
from estyma_local.web import start_web


class CloudFallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def make(self, env=None):
        return F.CloudFallback(self.dir, environ=env or {})

    def test_default_and_env(self):
        self.assertEqual(self.make().seconds, F.DEFAULT_SECONDS)
        self.assertLess(F.DEFAULT_SECONDS, F.DEVICE_GIVES_UP)
        self.assertEqual(self.make({"CLOUD_FALLBACK_SECONDS": "0"}).seconds, 0)
        self.assertEqual(self.make({"CLOUD_FALLBACK_SECONDS": "8"}).seconds, 8)
        self.assertEqual(self.make({"CLOUD_FALLBACK_SECONDS": "x"}).seconds, F.DEFAULT_SECONDS)
        self.assertEqual(self.make({"CLOUD_FALLBACK_SECONDS": "999"}).seconds, F.DEFAULT_SECONDS)

    def test_configure_persists(self):
        fb = self.make()
        st = fb.configure(9)
        self.assertEqual((st["enabled"], st["seconds"]), (True, 9))
        self.assertEqual(self.make({"CLOUD_FALLBACK_SECONDS": "3"}).seconds, 9)  # page wins
        fb.configure(0)
        self.assertFalse(self.make().status()["enabled"])
        with open(os.path.join(self.dir, "cloud-fallback.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["seconds"], 0)

    def test_configure_rejects(self):
        fb = self.make()
        for bad in (-1, 61, 2.5, "10", True, None):
            with self.assertRaises(ValueError):
                fb.configure(bad)
        self.assertEqual(fb.seconds, F.DEFAULT_SECONDS)

    def test_counts(self):
        fb = self.make()
        fb.answered_locally("slow", "x")
        fb.answered_locally("cloud_error")
        fb.late_reply()
        st = fb.status()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["counts"]["slow"], 1)
        self.assertEqual(st["last"]["reason"], "cloud_error")
        self.assertEqual(st["late_replies"], 1)


class CloudFallbackApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fb = F.CloudFallback(self.dir, environ={})
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()

    def ui(self, auth):
        user, pw = auth or (None, None)
        srv = start_web("127.0.0.1", 0, self.dir, user, pw, fallback=self.fb)
        self.servers.append(srv)
        return srv.server_address[1]

    def call(self, port, method, body=None, auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        conn.request(method, "/api/cloud-fallback",
                     body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data) if r.getheader("Content-Type") == "application/json" else None

    def test_change_needs_ui_auth(self):
        port = self.ui(None)
        code, st = self.call(port, "GET")
        self.assertEqual(code, 200)
        self.assertEqual(st["seconds"], F.DEFAULT_SECONDS)
        self.assertFalse(st["ui_auth"])
        code, _ = self.call(port, "POST", {"seconds": 5})
        self.assertEqual(code, 403)
        self.assertEqual(self.fb.seconds, F.DEFAULT_SECONDS)

    def test_change_with_auth(self):
        port = self.ui(("u", "p"))
        code, _ = self.call(port, "POST", {"seconds": 5})
        self.assertEqual(code, 401)
        code, res = self.call(port, "POST", {"seconds": 100}, auth="u:p")
        self.assertEqual(code, 400)
        code, res = self.call(port, "POST", {"seconds": 5}, auth="u:p")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"] and res["enabled"])
        self.assertEqual(self.fb.seconds, 5)
        code, res = self.call(port, "POST", {"seconds": 0}, auth="u:p")
        self.assertEqual((code, res["enabled"]), (200, False))

    def test_missing_without_fallback(self):
        srv = start_web("127.0.0.1", 0, self.dir)
        self.servers.append(srv)
        code, _ = self.call(srv.server_address[1], "GET")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
