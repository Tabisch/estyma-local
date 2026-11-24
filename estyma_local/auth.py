"""Web UI login: users, sessions and a limit on failed logins.

Two users:

* admin (``UI_USER`` / ``UI_PASSWORD``): everything, including writes to
  the heating, app settings and downloading or deleting logs,
* viewer (``UI_VIEWER_USER`` / ``UI_VIEWER_PASSWORD``, optional): every page
  and value, but no changes and no log downloads (the logs hold device IDs).

The login is always on; it cannot be switched off. Without ``UI_USER`` /
``UI_PASSWORD`` the server makes up a password for the user ``admin`` on
first start, keeps it in ``.ui-password`` in LOG_DIR (hidden from the logs
page) and prints it to the container log.

Browsers log in on ``/login`` and get a session cookie (HttpOnly,
SameSite=Strict, Secure behind HTTPS). Scripts can still send HTTP basic
auth with every request. Sessions are kept in ``.ui-sessions.json`` (only a
hash of each token), so a restart does not log anyone out.

After ``MAX_FAILURES`` failed logins within ``WINDOW`` seconds from one
address, that address is refused for ``LOCKOUT`` seconds (basic auth
included).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass

ADMIN, VIEWER = "admin", "viewer"
COOKIE = "estyma_session"
PASSWORD_FILE = ".ui-password"
SESSION_FILE = ".ui-sessions.json"

MAX_FAILURES = 5
WINDOW = 15 * 60
LOCKOUT = 15 * 60
MAX_SESSIONS = 200


class LoginError(Exception):
    """Wrong login or locked out; ``retry_after`` is set when locked out."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class User:
    name: str
    password: str
    role: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generated_password(log_dir: str) -> tuple[str, bool]:
    """(password, new) for the admin when none is configured. Kept in
    ``.ui-password`` (mode 600) so it survives restarts."""
    path = os.path.join(log_dir, PASSWORD_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            pw = fh.read().strip()
        if len(pw) >= 8:
            return pw, False
    except OSError:
        pass
    pw = secrets.token_urlsafe(12)
    os.makedirs(log_dir, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pw + "\n")
    return pw, True


class Auth:
    def __init__(self, users, *, log_dir: str | None = None, session_days: float = 30,
                 clock=time.time) -> None:
        self.users = [u for u in users if u.name and u.password]
        if len({u.name for u in self.users}) != len(self.users):
            raise ValueError("UI_USER and UI_VIEWER_USER must be different")
        self.log_dir = log_dir
        self.session_seconds = session_days * 86400
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict = {}    # token hash -> {"user", "role", "expires"}
        self._failures: dict = {}    # address -> [timestamps]
        self._locked: dict = {}      # address -> until
        self._load()

    # -- users --------------------------------------------------------------

    def check(self, name: str, password: str) -> User | None:
        """The user for a name and password, compared in constant time."""
        found = None
        for u in self.users:
            ok_name = hmac.compare_digest(u.name.encode(), (name or "").encode())
            ok_pw = hmac.compare_digest(u.password.encode(), (password or "").encode())
            if ok_name and ok_pw:
                found = u
        return found

    # -- failed login limit -------------------------------------------------

    def locked_for(self, addr: str) -> float:
        with self._lock:
            until = self._locked.get(addr, 0)
            left = until - self.clock()
            if left <= 0:
                self._locked.pop(addr, None)
                return 0
            return left

    def _failed(self, addr: str) -> None:
        now = self.clock()
        with self._lock:
            times = [t for t in self._failures.get(addr, []) if now - t < WINDOW] + [now]
            self._failures[addr] = times
            if len(times) >= MAX_FAILURES:
                self._locked[addr] = now + LOCKOUT
                self._failures.pop(addr, None)
                print(f"web UI: {len(times)} failed logins from {addr}, refused for "
                      f"{LOCKOUT // 60} min", flush=True)
            if len(self._failures) > 1000:   # do not grow without bound
                for a in sorted(self._failures, key=lambda a: self._failures[a][-1])[:500]:
                    del self._failures[a]

    def attempt(self, name: str, password: str, addr: str) -> User:
        """Check a login from ``addr``; raises LoginError."""
        left = self.locked_for(addr)
        if left:
            raise LoginError(f"too many failed logins, try again in {left / 60:.0f} min", left)
        user = self.check(name, password)
        if user is None:
            self._failed(addr)
            left = self.locked_for(addr)
            if left:
                raise LoginError(f"too many failed logins, try again in {left / 60:.0f} min", left)
            raise LoginError("wrong user name or password")
        with self._lock:
            self._failures.pop(addr, None)
        return user

    def basic(self, header: str, addr: str) -> User | None:
        """``Authorization: Basic ...`` -> user; failures count like logins."""
        if not header.startswith("Basic "):
            return None
        try:
            name, _, pw = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            name, pw = "", ""
        try:
            return self.attempt(name, pw, addr)
        except LoginError:
            return None

    # -- sessions -----------------------------------------------------------

    def login(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._expire(now)
            if len(self._sessions) >= MAX_SESSIONS:   # drop the oldest
                oldest = min(self._sessions, key=lambda h: self._sessions[h]["expires"])
                del self._sessions[oldest]
            self._sessions[_hash(token)] = {"user": user.name, "role": user.role,
                                            "expires": now + self.session_seconds}
        self._save()
        return token

    def session(self, token: str | None) -> User | None:
        if not token:
            return None
        now = self.clock()
        with self._lock:
            s = self._sessions.get(_hash(token))
            if s is None or s["expires"] <= now:
                return None
        # A user removed or changed in the configuration loses its sessions.
        for u in self.users:
            if u.name == s["user"] and u.role == s["role"]:
                return u
        return None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            gone = self._sessions.pop(_hash(token), None)
        if gone:
            self._save()

    def _expire(self, now: float) -> None:
        for h in [h for h, s in self._sessions.items() if s["expires"] <= now]:
            del self._sessions[h]

    def _path(self) -> str | None:
        return os.path.join(self.log_dir, SESSION_FILE) if self.log_dir else None

    def _load(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for h, s in data.items():
                if isinstance(s, dict) and {"user", "role", "expires"} <= s.keys():
                    self._sessions[h] = s
        self._expire(self.clock())

    def _save(self) -> None:
        path = self._path()
        if not path:
            return
        with self._lock:
            data = dict(self._sessions)
        tmp = path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError as exc:
            print(f"web UI: saving sessions failed: {exc}", flush=True)


def from_settings(user, password, viewer_user, viewer_password, log_dir: str,
                  session_days: float = 30) -> Auth:
    """The Auth for the configuration (the login is always on)."""
    if not (user and password):
        user = user or "admin"
        password, new = generated_password(log_dir)
        where = os.path.join(log_dir, PASSWORD_FILE)
        if new:
            print(f"web UI: no UI_USER / UI_PASSWORD set, created a login: user {user!r}, "
                  f"password {password!r} (kept in {where}; set UI_PASSWORD to choose your own)",
                  flush=True)
        else:
            print(f"web UI: login user {user!r}, password in {where} "
                  "(set UI_USER / UI_PASSWORD to choose your own)", flush=True)
    users = [User(user, password, ADMIN)]
    if viewer_user and viewer_password:
        users.append(User(viewer_user, viewer_password, VIEWER))
    return Auth(users, log_dir=log_dir, session_days=session_days)
