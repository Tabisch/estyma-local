"""Cloud monitor: toggle, persistence, auth and polling against a fake wrapper."""

import base64
import http.client
import json
import os
import tempfile
import time
import unittest

import fixtures  # noqa: F401  (puts proxy/ on sys.path)

import cloud_monitor as CM
from log_ui import start_log_ui

ENV = {"ESTYMA_EMAIL": "user@example.com", "ESTYMA_PASSWORD": "secret"}


class FakeResp:
    def __init__(self, obj):
        self.obj = obj

    async def json(self, content_type=None):
        return self.obj


class FakeApi:
    """The parts of EstymaApi the recorder uses."""
    update_url = "{0}/update"
    _http_url = "https://cloud.example"
    fetchDevicedataBody = "id={0}"
    fail_login = False
    logins = 0

    def __init__(self):
        self.devices = {}

    async def initialize(self):
        type(self).logins += 1
        if self.fail_login:
            raise Exception("login failed")
        self.devices = {"123": {"name": "boiler"}}

    async def getDevices(self):
        return self.devices

    async def getAvailableSettings(self):
        return {}

    async def getDeviceData(self, textToValues=False):
        return {"123": {"temp_boiler_sub1": 70.5, "online": {"diffSeconds": 12}}}

    async def _makeRequest(self, method, url, data=None):
        return FakeResp({"kociol_temp_sub1": "70,5", "palnik_status_sub1_int": "5",
                         "online": {"diff": "0d 0h:0m:12s"}})

    async def _logout(self):
        pass


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class CloudMonitorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        FakeApi.fail_login, FakeApi.logins = False, 0

    async def _diag(self, *a):
        return "wrong password"

    def make(self, env=ENV):
        mon = CM.CloudMonitor(self.dir, environ=env, interval=0.05,
                              api_factory=FakeApi, diagnose=self._diag)
        mon.start()
        return mon

    def test_unconfigured_cannot_be_switched_on(self):
        mon = self.make(env={})
        self.assertFalse(mon.configured)
        with self.assertRaises(ValueError):
            mon.set_enabled(True)
        self.assertTrue(wait_for(lambda: mon.status()["state"] == "unconfigured"))
        self.assertIn("ESTYMA_EMAIL", mon.status()["problem"])

    def test_default_off_then_polls_and_persists(self):
        mon = self.make()
        self.assertTrue(mon.configured)
        self.assertFalse(mon.enabled)
        time.sleep(0.1)
        self.assertEqual(FakeApi.logins, 0)
        mon.set_enabled(True)
        self.assertTrue(wait_for(lambda: mon.status()["polls"] >= 2))
        st = mon.status()
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["devices"]["123"]["values"], {"boiler": 70.5, "burner_state": 5})
        self.assertEqual(st["devices"]["123"]["data_age_s"], 12)
        self.assertNotIn("secret", json.dumps(st))
        self.assertTrue(any(n.startswith("cloud-") and n.endswith(".jsonl") for n in os.listdir(self.dir)))
        with open(os.path.join(self.dir, "cloud-monitor.json")) as fh:
            self.assertTrue(json.load(fh)["enabled"])
        mon.set_enabled(False)
        self.assertTrue(wait_for(lambda: mon.status()["state"] == "off"))
        polls = mon.status()["polls"]
        time.sleep(0.2)
        self.assertEqual(mon.status()["polls"], polls)
        # A restart keeps the saved choice, not CLOUD_MONITOR.
        again = CM.CloudMonitor(self.dir, environ={**ENV, "CLOUD_MONITOR": "1"}, api_factory=FakeApi)
        self.assertFalse(again.enabled)

    def test_env_default_on(self):
        mon = CM.CloudMonitor(self.dir, environ={**ENV, "CLOUD_MONITOR": "1"}, api_factory=FakeApi)
        self.assertTrue(mon.enabled)

    def test_password_b64(self):
        env = {"ESTYMA_EMAIL": "a@b", "ESTYMA_PASSWORD_B64": base64.b64encode(b" pw").decode()}
        mon = CM.CloudMonitor(self.dir, environ=env, api_factory=FakeApi)
        self.assertTrue(mon.configured)
        self.assertEqual(mon._password, " pw")

    def test_login_failure_reported(self):
        FakeApi.fail_login = True
        mon = self.make()
        mon.set_enabled(True)
        self.assertTrue(wait_for(lambda: mon.status()["state"] == "error"))
        self.assertEqual(mon.status()["message"], "login failed: wrong password")
        mon.set_enabled(False)  # also interrupts the retry wait
        self.assertTrue(wait_for(lambda: mon.status()["state"] == "off"))


class CloudApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mon = CM.CloudMonitor(self.dir, environ=ENV, interval=0.05, api_factory=FakeApi)
        self.mon.start()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()

    def ui(self, auth):
        user, pw = auth or (None, None)
        srv = start_log_ui("127.0.0.1", 0, self.dir, user, pw, cloud=self.mon)
        self.servers.append(srv)
        return srv.server_address[1]

    def call(self, port, method, body=None, auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        conn.request(method, "/api/cloud", body=json.dumps(body) if body is not None else None,
                     headers=headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data) if r.status != 401 else None

    def test_toggle_needs_ui_auth(self):
        port = self.ui(None)
        code, st = self.call(port, "GET")
        self.assertEqual(code, 200)
        self.assertTrue(st["configured"])
        self.assertFalse(st["ui_auth"])
        code, res = self.call(port, "POST", {"enabled": True})
        self.assertEqual(code, 403)
        self.assertFalse(self.mon.enabled)

    def test_toggle_with_auth(self):
        port = self.ui(("u", "p"))
        code, _ = self.call(port, "POST", {"enabled": True})
        self.assertEqual(code, 401)
        code, res = self.call(port, "POST", {"enabled": "yes"}, auth="u:p")
        self.assertEqual(code, 400)
        code, res = self.call(port, "POST", {"enabled": True}, auth="u:p")
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"] and res["enabled"])
        self.assertTrue(wait_for(lambda: self.mon.status()["polls"] >= 1))
        code, res = self.call(port, "POST", {"enabled": False}, auth="u:p")
        self.assertFalse(res["enabled"])


if __name__ == "__main__":
    unittest.main()
