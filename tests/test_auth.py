"""Web UI login: sessions, failed-login limit, read-only user, CSRF guards."""

import base64
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from fixtures import report

from estyma_local import auth as A
from estyma_local.controller import Controller
from estyma_local.web import start_web


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def users():
    return [A.User("admin", "secret-1", A.ADMIN), A.User("guest", "secret-2", A.VIEWER)]


class AuthUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        self.a = A.Auth(users(), log_dir=self.dir, clock=self.clock)

    def test_check(self):
        self.assertEqual(self.a.check("admin", "secret-1").role, A.ADMIN)
        self.assertEqual(self.a.check("guest", "secret-2").role, A.VIEWER)
        self.assertIsNone(self.a.check("admin", "secret-2"))
        self.assertIsNone(self.a.check("", ""))

    def test_lockout_after_failures(self):
        for _ in range(A.MAX_FAILURES - 1):
            with self.assertRaisesRegex(A.LoginError, "wrong"):
                self.a.attempt("admin", "nope", "10.0.0.5")
        with self.assertRaises(A.LoginError) as cm:
            self.a.attempt("admin", "nope", "10.0.0.5")
        self.assertTrue(cm.exception.retry_after)
        # even the right password is refused while locked, other addresses are not
        with self.assertRaisesRegex(A.LoginError, "too many"):
            self.a.attempt("admin", "secret-1", "10.0.0.5")
        self.a.attempt("admin", "secret-1", "10.0.0.6")
        self.clock.t += A.LOCKOUT + 1
        self.a.attempt("admin", "secret-1", "10.0.0.5")

    def test_failures_expire(self):
        for _ in range(A.MAX_FAILURES - 1):
            with self.assertRaises(A.LoginError):
                self.a.attempt("admin", "nope", "x")
        self.clock.t += A.WINDOW + 1
        with self.assertRaisesRegex(A.LoginError, "wrong"):
            self.a.attempt("admin", "nope", "x")

    def test_basic_counts_as_failure(self):
        bad = "Basic " + base64.b64encode(b"admin:nope").decode()
        for _ in range(A.MAX_FAILURES):
            self.assertIsNone(self.a.basic(bad, "y"))
        good = "Basic " + base64.b64encode(b"admin:secret-1").decode()
        self.assertIsNone(self.a.basic(good, "y"))     # locked out
        self.assertEqual(self.a.basic(good, "z").name, "admin")

    def test_sessions_survive_restart_and_expire(self):
        tok = self.a.login(self.a.check("guest", "secret-2"))
        again = A.Auth(users(), log_dir=self.dir, clock=self.clock)
        self.assertEqual(again.session(tok).name, "guest")
        with open(os.path.join(self.dir, A.SESSION_FILE)) as fh:
            self.assertNotIn(tok, fh.read())               # only a hash is stored
        self.clock.t += 31 * 86400
        self.assertIsNone(again.session(tok))

    def test_logout_and_changed_user(self):
        tok = self.a.login(self.a.check("admin", "secret-1"))
        other = A.Auth([A.User("someone", "x" * 9, A.ADMIN)], log_dir=self.dir, clock=self.clock)
        self.assertIsNone(other.session(tok))              # user no longer configured
        self.a.logout(tok)
        self.assertIsNone(self.a.session(tok))

    def test_generated_password(self):
        a = A.from_settings(True, None, None, None, None, self.dir)
        pw = open(os.path.join(self.dir, A.PASSWORD_FILE)).read().strip()
        self.assertGreaterEqual(len(pw), 12)
        self.assertEqual(a.check("admin", pw).role, A.ADMIN)
        self.assertEqual(oct(os.stat(os.path.join(self.dir, A.PASSWORD_FILE)).st_mode & 0o777), "0o600")
        b = A.from_settings(True, None, None, None, None, self.dir)   # same password after a restart
        self.assertIsNotNone(b.check("admin", pw))
        self.assertIsNone(A.from_settings(False, "a", "b", None, None, self.dir))
        c = A.from_settings(True, "me", "mine-123", "v", "view-123", self.dir)
        self.assertEqual([u.role for u in c.users], [A.ADMIN, A.VIEWER])


class WebLoginTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(self.dir, "exchanges-2026-11-01.jsonl"), "w") as fh:
            fh.write("{}\n")
        self.ctl = Controller("hybrid", self.dir, state_file=f"{self.dir}/s.json",
                              writes_enabled=True)
        self.ctl.observe_report(report())
        self.authn = A.Auth(users(), log_dir=self.dir)
        self.ui = start_web("127.0.0.1", 0, self.dir, controller=self.ctl, authn=self.authn)
        self.addCleanup(self.ui.shutdown)
        self.base = f"http://127.0.0.1:{self.ui.server_address[1]}"

    def req(self, path, body=None, cookie=None, headers=None):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with OPENER.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def login(self, user, pw, **kw):
        code, h, body = self.req("/api/login", {"user": user, "password": pw, **kw})
        cookie = (h.get("Set-Cookie") or "").split(";")[0]
        return code, cookie, json.loads(body)

    def test_pages_redirect_to_login_api_says_401(self):
        code, h, _ = self.req("/heating?x=1")
        self.assertEqual(code, 303)
        self.assertEqual(h["Location"], "/login?next=%2Fheating%3Fx%3D1")
        self.assertEqual(self.req("/api/settings")[0], 401)
        self.assertEqual(self.req("/files/exchanges-2026-11-01.jsonl")[0], 401)
        code, h, body = self.req("/login")
        self.assertEqual(code, 200)
        self.assertIn(b"/api/login", body)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(self.req("/api/health")[0], 200)

    def test_login_cookie_and_logout(self):
        code, cookie, body = self.login("admin", "secret-1", next="/heating")
        self.assertEqual((code, body["ok"], body["next"]), (200, True, "/heating"))
        _, h, _ = self.req("/api/login", {"user": "admin", "password": "secret-1"})
        attrs = h["Set-Cookie"]
        for a in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(a, attrs)
        self.assertNotIn("Secure", attrs)
        _, h, _ = self.req("/api/login", {"user": "admin", "password": "secret-1"},
                           headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", h["Set-Cookie"])
        code, _, body = self.req("/api/settings", cookie=cookie)
        self.assertEqual(code, 200)
        snap = json.loads(body)
        self.assertEqual((snap["ui_auth"], snap["ui_role"]), (True, "admin"))
        code, _, page = self.req("/heating", cookie=cookie)
        self.assertIn(b"data-logout", page)
        code, h, _ = self.req("/logout", cookie=cookie)
        self.assertEqual((code, h["Location"]), (303, "/login"))
        self.assertEqual(self.req("/api/settings", cookie=cookie)[0], 401)

    def test_open_redirect_refused(self):
        for bad in ("//evil.example/", "https://evil.example/", "evil", "/\\evil"):
            _, _, body = self.login("admin", "secret-1", next=bad)
            self.assertEqual(body["next"], "/", bad)

    def test_wrong_password_and_lockout(self):
        code, cookie, body = self.login("admin", "nope")
        self.assertEqual((code, cookie), (401, ""))
        for _ in range(A.MAX_FAILURES - 1):
            code, _, _ = self.login("admin", "nope")
        self.assertEqual(code, 429)
        self.assertEqual(self.login("admin", "secret-1")[0], 429)

    def test_viewer_is_read_only(self):
        _, cookie, body = self.login("guest", "secret-2")
        self.assertEqual(body["role"], "viewer")
        code, _, snap = self.req("/api/settings", cookie=cookie)
        snap = json.loads(snap)
        self.assertEqual((code, snap["ui_auth"], snap["ui_role"]), (200, False, "viewer"))
        code, _, body = self.req("/api/set", {"key": "boiler_target", "value": 62}, cookie=cookie)
        self.assertEqual(code, 403)
        self.assertIn("read-only login", json.loads(body)["message"])
        self.assertEqual(self.ctl.pending, {})
        self.assertEqual(self.req("/api/snapshots/save", {"supplier": "x"}, cookie=cookie)[0], 403)
        self.assertEqual(self.req("/api/read-settings", {}, cookie=cookie)[0], 200)
        self.assertEqual(self.req("/files/exchanges-2026-11-01.jsonl", cookie=cookie)[0], 403)
        self.assertEqual(self.req("/all.zip", cookie=cookie)[0], 403)
        code, _, page = self.req("/logs", cookie=cookie)
        self.assertNotIn(b'href="/files/', page)
        self.assertNotIn(b"/all.zip", page)

    def test_admin_can_write_and_download(self):
        _, cookie, _ = self.login("admin", "secret-1")
        code, _, _ = self.req("/api/set", {"key": "boiler_target", "value": 62}, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(self.ctl.pending["boiler_target"].value, 62)
        self.assertEqual(self.req("/files/exchanges-2026-11-01.jsonl", cookie=cookie)[0], 200)

    def test_basic_auth_still_works_for_scripts(self):
        h = {"Authorization": "Basic " + base64.b64encode(b"admin:secret-1").decode()}
        self.assertEqual(self.req("/api/settings", headers=h)[0], 200)

    def test_cross_site_post_refused(self):
        _, cookie, _ = self.login("admin", "secret-1")
        code, _, body = self.req("/api/set", {"key": "boiler_target", "value": 62}, cookie=cookie,
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.ctl.pending, {})
        host = self.base.split("//")[1]
        code, _, _ = self.req("/api/set", {"key": "boiler_target", "value": 62}, cookie=cookie,
                              headers={"Origin": "http://" + host})
        self.assertEqual(code, 200)

    def test_hidden_files_never_served(self):
        _, cookie, _ = self.login("admin", "secret-1")
        self.assertEqual(self.req("/files/.ui-sessions.json", cookie=cookie)[0], 404)
        code, _, page = self.req("/logs", cookie=cookie)
        self.assertNotIn(b".ui-sessions", page)


class LoginOffTest(unittest.TestCase):
    def test_open_and_read_only(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ctl = Controller("hybrid", d, state_file=f"{d}/s.json", writes_enabled=True)
        ctl.observe_report(report())
        ui = start_web("127.0.0.1", 0, d, controller=ctl)
        self.addCleanup(ui.shutdown)
        base = f"http://127.0.0.1:{ui.server_address[1]}"
        with urllib.request.urlopen(base + "/api/settings") as r:
            self.assertFalse(json.load(r)["ui_auth"])
        r = urllib.request.Request(base + "/api/set", data=b'{"key":"boiler_target","value":62}',
                                   headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("UI_AUTH=1", json.loads(cm.exception.read())["message"])


if __name__ == "__main__":
    unittest.main()
