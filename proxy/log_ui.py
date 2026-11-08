"""Tiny web UI: live monitor of the controller's values and log downloads.

``/`` shows the monitor (monitor.py, monitor.html), ``/logs`` lists the log
files. Runs inside the proxy process on its own port (UI_PORT, default 8089), never
on the proxy port. Read-only: it only serves files that sit directly in
LOG_DIR. Optional HTTP basic auth via UI_USER / UI_PASSWORD.

Logs contain whatever the controller sends (device IDs, PINs, ...). Keep the
port on your LAN.
"""

from __future__ import annotations

import base64
import hmac
import html
import io
import json
import os
import threading
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estyma logs</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; padding: 0 1rem; color: #222; }}
 h1 {{ font-size: 1.4rem; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd; }}
 td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 a.button {{ display: inline-block; margin: .5rem 0 1rem; padding: .4rem .8rem; background: #2563eb; color: #fff; border-radius: 4px; text-decoration: none; }}
 .muted {{ color: #666; font-size: .9rem; }}
 @media (prefers-color-scheme: dark) {{ body {{ background: #111; color: #ddd; }} th, td {{ border-color: #333; }} a {{ color: #7aa7ff; }} }}
</style></head><body>
<p><a href="/">Monitor</a> &nbsp; <b>Logs</b></p>
<h1>Estyma logs</h1>
<p class="muted">{count} files, {total} in <code>{logdir}</code></p>
{zip_link}
<table><thead><tr><th>File</th><th>Size</th><th>Modified</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>
"""


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class LogUIHandler(BaseHTTPRequestHandler):
    server_version = "estyma-log-ui"
    log_dir: str
    auth: bytes | None  # expected "user:password" or None
    monitor = None      # monitor.Monitor or None
    controller = None   # controller.Controller or None

    def log_message(self, fmt, *args):
        pass

    # -- helpers ------------------------------------------------------------

    def _authorized(self) -> bool:
        if self.auth is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:].strip())
        except ValueError:
            return False
        return hmac.compare_digest(given, self.auth)

    def _files(self):
        try:
            names = os.listdir(self.log_dir)
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            path = os.path.join(self.log_dir, name)
            if os.path.isfile(path) and not name.startswith("."):
                st = os.stat(path)
                out.append((name, st.st_size, st.st_mtime))
        return sorted(out, key=lambda f: f[2], reverse=True)

    def _send(self, code: int, body: bytes, ctype: str, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- routes -------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._authorized():
            self._send(401, b"authentication required\n", "text/plain",
                       [("WWW-Authenticate", 'Basic realm="estyma logs"')])
            return
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/":
            return self._monitor_page() if self.monitor else self._index()
        if path in ("/logs", "/logs/"):
            return self._index()
        if path == "/api/monitor" and self.monitor:
            return self._monitor_api(url.query)
        if path == "/api/control" and self.controller:
            snap = self.controller.snapshot()
            snap["ui_auth"] = self.auth is not None
            return self._json(200, snap)
        if path == "/all.zip":
            return self._zip()
        if path.startswith("/files/"):
            return self._file(urllib.parse.unquote(path[len("/files/"):]))
        self._send(404, b"not found\n", "text/plain")

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def do_POST(self):
        if not self._authorized():
            self._send(401, b"authentication required\n", "text/plain",
                       [("WWW-Authenticate", 'Basic realm="estyma logs"')])
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/set", "/api/cancel") or not self.controller:
            return self._send(404, b"not found\n", "text/plain")
        # A JSON content type cannot be sent cross-site without a CORS
        # preflight, which we never answer: a simple CSRF guard.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"ok": False, "message": "Content-Type must be application/json"})
        if self.auth is None:
            return self._json(403, {"ok": False, "message":
                                    "set UI_USER and UI_PASSWORD to allow writes from the web UI"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            key = str(req.get("key", ""))
        except (ValueError, AttributeError):
            return self._json(400, {"ok": False, "message": "invalid JSON"})
        from controller import WriteError
        if path == "/api/cancel":
            ok = self.controller.cancel_write(key, source="web")
            return self._json(200, {"ok": ok, "message": "cancelled" if ok else "nothing pending"})
        try:
            res = self.controller.request_write(key, req.get("value"), source="web")
        except WriteError as exc:
            return self._json(409, {"ok": False, "message": str(exc)})
        return self._json(200, {"ok": True, **res})

    def _index(self):
        files = self._files()
        rows = "".join(
            f'<tr><td><a href="/files/{urllib.parse.quote(n)}">{html.escape(n)}</a></td>'
            f'<td class="num">{human_size(s)}</td>'
            f'<td>{datetime.fromtimestamp(m):%Y-%m-%d %H:%M:%S}</td></tr>'
            for n, s, m in files
        ) or '<tr><td colspan="3" class="muted">No logs yet.</td></tr>'
        body = PAGE.format(
            count=len(files),
            total=human_size(sum(s for _, s, _ in files)),
            logdir=html.escape(self.log_dir),
            zip_link='<a class="button" href="/all.zip">Download all (zip)</a>' if files else "",
            rows=rows,
        ).encode()
        self._send(200, body, "text/html; charset=utf-8")

    def _monitor_page(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.html"), "rb") as fh:
            self._send(200, fh.read(), "text/html; charset=utf-8")

    def _monitor_api(self, query: str):
        try:
            hours = float(urllib.parse.parse_qs(query).get("hours", ["24"])[0])
        except ValueError:
            hours = 24
        hours = min(max(hours, 0.1), 24 * 30)
        body = json.dumps(self.monitor.snapshot(hours), separators=(",", ":")).encode()
        self._send(200, body, "application/json")

    def _file(self, name: str):
        # Only plain file names directly inside LOG_DIR.
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return self._send(404, b"not found\n", "text/plain")
        path = os.path.join(self.log_dir, name)
        if not os.path.isfile(path):
            return self._send(404, b"not found\n", "text/plain")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            # Files may still be growing; send exactly the advertised size.
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, _, _ in self._files():
                zf.write(os.path.join(self.log_dir, name), arcname=name)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._send(200, buf.getvalue(), "application/zip",
                   [("Content-Disposition", f'attachment; filename="estyma-logs-{stamp}.zip"')])


def start_log_ui(host: str, port: int, log_dir: str,
                 user: str | None = None, password: str | None = None,
                 monitor=None, controller=None) -> ThreadingHTTPServer:
    """Start the UI in a background thread and return the server."""
    auth = f"{user}:{password}".encode() if user and password else None
    handler = type("Handler", (LogUIHandler,), {"log_dir": os.path.abspath(log_dir), "auth": auth,
                                                "monitor": monitor, "controller": controller})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="log-ui", daemon=True).start()
    return server
