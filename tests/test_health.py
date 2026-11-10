"""/api/health and the Docker healthcheck."""

import json
import socket
import tempfile
import unittest
import urllib.request

from estyma_local import healthcheck
from estyma_local.controller import Controller
from estyma_local.web import start_web


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ctl = Controller("proxy", self.dir, state_file=f"{self.dir}/s.json")
        # With UI auth on: the health endpoint must still answer without it.
        self.ui = start_web("127.0.0.1", 0, self.dir, "u", "p", controller=self.ctl)
        self.addCleanup(self.ui.shutdown)
        self.ui_port = self.ui.server_address[1]

    def test_health_needs_no_login(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ui_port}/api/health") as r:
            body = json.load(r)
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "proxy")
        self.assertIsNone(body["last_report_age_s"])

    def test_other_pages_still_need_login(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{self.ui_port}/api/control")
        self.assertEqual(cm.exception.code, 401)

    def test_healthcheck(self):
        # The web UI's port also stands in for the controller port here.
        env = {"LISTEN_PORT": str(self.ui_port), "UI_PORT": str(self.ui_port)}
        self.assertIsNone(healthcheck.check(env))
        dead = str(free_port())
        self.assertIn("controller port", healthcheck.check({"LISTEN_PORT": dead, "UI_PORT": "0"}))
        self.assertIn("web UI", healthcheck.check({"LISTEN_PORT": str(self.ui_port), "UI_PORT": dead}))
        self.assertIsNone(healthcheck.check({"LISTEN_PORT": str(self.ui_port), "UI_PORT": "0"}))


if __name__ == "__main__":
    unittest.main()
