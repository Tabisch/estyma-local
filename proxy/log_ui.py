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
import time
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
 a.button, button {{ display: inline-block; margin: .5rem .4rem 1rem 0; padding: .4rem .8rem; background: #2563eb; color: #fff; border: 0; border-radius: 4px; text-decoration: none; font: inherit; cursor: pointer; }}
 button.del {{ background: none; color: #c62828; border: 1px solid currentColor; padding: .15rem .55rem; margin: 0; font-size: .85rem; }}
 button.delall {{ background: #c62828; }}
 button:disabled {{ opacity: .45; cursor: not-allowed; }}
 #msg {{ color: #c62828; min-height: 1.2em; }}
 .muted {{ color: #666; font-size: .9rem; }}
 @media (prefers-color-scheme: dark) {{ body {{ background: #111; color: #ddd; }} th, td {{ border-color: #333; }} a {{ color: #7aa7ff; }} }}
</style></head><body>
<p><a href="/" data-i18n="monitor">Monitor</a> &nbsp; <a href="/heating" data-i18n="settings">Heating settings</a> &nbsp; <a href="/registers" data-i18n="registers">Registers</a> &nbsp; <b data-i18n="logs">Logs</b></p>
<h1 data-i18n="title">Estyma logs</h1>
<p class="muted"><span data-i18n="files" data-n="{count}">{count} files</span>, {total} <span data-i18n="in">in</span> <code>{logdir}</code></p>
{zip_link}{delall}
<p id="msg" class="muted">{delnote}</p>
<table><thead><tr><th data-i18n="file">File</th><th data-i18n="size">Size</th><th data-i18n="modified">Modified</th><th></th></tr></thead>
<tbody>{rows}</tbody></table>
<script>
// Delete buttons: POST JSON (needs UI_USER / UI_PASSWORD on the server).
(function () {{
  function ask(names) {{
    var de = document.documentElement.lang === "de";
    var q = names.length === 1
      ? (de ? names[0] + " endgültig löschen?" : "Delete " + names[0] + " for good?")
      : (de ? "Alle " + names.length + " .jsonl-Dateien endgültig löschen?" : "Delete all " + names.length + " .jsonl files for good?");
    if (!confirm(q)) return;
    fetch("/api/delete-logs", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify({{ names: names }}) }})
      .then(function (r) {{ return r.json(); }})
      .then(function (j) {{ if (j.ok) location.reload(); else document.getElementById("msg").textContent = j.message; }})
      .catch(function (e) {{ document.getElementById("msg").textContent = String(e); }});
  }}
  document.addEventListener("click", function (e) {{
    var b = e.target.closest("button[data-del], button.delall");
    if (!b) return;
    if (b.dataset.del) return ask([b.dataset.del]);
    ask(Array.prototype.map.call(document.querySelectorAll("button[data-del]"), function (x) {{ return x.dataset.del; }}));
  }});
}})();
</script>
<script>
// Same language choice as the monitor page (stored in this browser).
(function () {{
  var de = {{ monitor: "Monitor", settings: "Heizungseinstellungen", registers: "Register", logs: "Logs", title: "Estyma-Logs", files: "{{n}} Dateien", "in": "in",
    file: "Datei", size: "Größe", modified: "Geändert", nologs: "Noch keine Logs.", zip: "Alle herunterladen (zip)",
    del: "Löschen", delall: "Alle .jsonl löschen", needauth: "Zum Löschen UI_USER und UI_PASSWORD setzen." }};
  var lang = "";
  try {{ lang = localStorage.getItem("estyma-lang") || ""; }} catch (e) {{}}
  if (!lang) lang = String((navigator.languages || [navigator.language || "en"])[0]).slice(0, 2).toLowerCase();
  if (lang !== "de") return;
  document.documentElement.lang = "de";
  document.querySelectorAll("[data-i18n]").forEach(function (el) {{
    var t = de[el.dataset.i18n];
    if (t) el.textContent = t.replace("{{n}}", el.dataset.n || "");
  }});
}})();
</script>
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
    cloud = None        # cloud_monitor.CloudMonitor or None

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
        if path in ("/registers", "/registers/") and self.monitor:
            return self._page("registers.html")
        if path == "/api/registers" and self.monitor:
            return self._json(200, {"now": time.time(), "loading": self.monitor.loading,
                                    "registers": self.monitor.registers.snapshot()})
        if path == "/api/control" and self.controller:
            snap = self.controller.snapshot()
            snap["ui_auth"] = self.auth is not None
            return self._json(200, snap)
        if path in ("/heating", "/heating/") and self.controller:
            return self._page("settings.html")
        if path == "/api/settings" and self.controller:
            snap = self.controller.settings_snapshot()
            snap["ui_auth"] = self.auth is not None
            return self._json(200, snap)
        if path == "/api/cloud" and self.cloud:
            st = self.cloud.status()
            st["ui_auth"] = self.auth is not None
            return self._json(200, st)
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
        available = {"/api/delete-logs": True, "/api/set": bool(self.controller),
                     "/api/cancel": bool(self.controller), "/api/cloud": bool(self.cloud),
                     "/api/read-settings": bool(self.controller)}
        if not available.get(path):
            return self._send(404, b"not found\n", "text/plain")
        # A JSON content type cannot be sent cross-site without a CORS
        # preflight, which we never answer: a simple CSRF guard.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"ok": False, "message": "Content-Type must be application/json"})
        if path == "/api/read-settings":  # only adds reads, changes nothing
            res = self.controller.request_settings_read()
            return self._json(200 if res["ok"] else 409, res)
        if self.auth is None:
            what = {"/api/delete-logs": "deleting logs",
                    "/api/cloud": "switching cloud monitoring"}.get(path, "writes")
            return self._json(403, {"ok": False, "message":
                                    f"set UI_USER and UI_PASSWORD to allow {what} from the web UI"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            key = str(req.get("key", ""))
        except (ValueError, AttributeError):
            return self._json(400, {"ok": False, "message": "invalid JSON"})
        if path == "/api/delete-logs":
            return self._delete_logs(req.get("names"))
        if path == "/api/cloud":
            if not isinstance(req.get("enabled"), bool):
                return self._json(400, {"ok": False, "message": "enabled must be true or false"})
            try:
                st = self.cloud.set_enabled(req["enabled"])
            except ValueError as exc:
                return self._json(409, {"ok": False, "message": str(exc)})
            st["ui_auth"] = True
            return self._json(200, {"ok": True, **st})
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
            f'<td>{datetime.fromtimestamp(m):%Y-%m-%d %H:%M:%S}</td>'
            f'<td>{self._del_button(n)}</td></tr>'
            for n, s, m in files
        ) or '<tr><td colspan="4" class="muted" data-i18n="nologs">No logs yet.</td></tr>'
        deletable = any(n.endswith(".jsonl") for n, _, _ in files)
        body = PAGE.format(
            delall=('<button class="delall" data-i18n="delall"%s>Delete all .jsonl</button>'
                    % ("" if self.auth else " disabled")) if deletable else "",
            delnote=('' if self.auth or not deletable else
                     '<span data-i18n="needauth">Set UI_USER and UI_PASSWORD to delete files.</span>'),
            count=len(files),
            total=human_size(sum(s for _, s, _ in files)),
            logdir=html.escape(self.log_dir),
            zip_link='<a class="button" href="/all.zip" data-i18n="zip">Download all (zip)</a>' if files else "",
            rows=rows,
        ).encode()
        self._send(200, body, "text/html; charset=utf-8")

    def _del_button(self, name: str) -> str:
        if not name.endswith(".jsonl"):
            return ""  # e.g. state.json is the server's state, not a log
        dis = "" if self.auth else " disabled"
        return (f'<button class="del" data-del="{html.escape(name, quote=True)}" '
                f'data-i18n="del"{dis}>Delete</button>')

    def _delete_logs(self, names) -> None:
        if not isinstance(names, list) or not names:
            return self._json(400, {"ok": False, "message": "names must be a non-empty list"})
        existing = {n for n, _, _ in self._files()}
        deleted, errors = [], []
        for name in names:
            name = str(name)
            # Only .jsonl files directly inside LOG_DIR, never state.json etc.
            if (not name.endswith(".jsonl") or "/" in name or "\\" in name
                    or name.startswith(".") or name not in existing):
                errors.append(f"{name}: not a log file")
                continue
            try:
                os.remove(os.path.join(self.log_dir, name))
                deleted.append(name)
            except OSError as exc:
                errors.append(f"{name}: {exc.strerror}")
        if deleted:
            print(f"log UI: deleted {', '.join(deleted)}", flush=True)
        self._json(200 if not errors else 409,
                   {"ok": not errors, "deleted": deleted, "message": "; ".join(errors) or "deleted"})

    def _monitor_page(self):
        self._page("monitor.html")

    def _page(self, name: str):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "rb") as fh:
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
                 monitor=None, controller=None, cloud=None) -> ThreadingHTTPServer:
    """Start the UI in a background thread and return the server."""
    auth = f"{user}:{password}".encode() if user and password else None
    handler = type("Handler", (LogUIHandler,), {"log_dir": os.path.abspath(log_dir), "auth": auth,
                                                "monitor": monitor, "controller": controller,
                                                "cloud": cloud})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="log-ui", daemon=True).start()
    return server
