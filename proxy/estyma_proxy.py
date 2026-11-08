#!/usr/bin/env python3
"""Transparent logging HTTP proxy for Estyma heating controller traffic.

The heating controller talks plain HTTP to the provider's server. Point the
controller at this proxy (DNS override or a NAT rule on the router), and it
forwards every request unchanged to the real server while writing each
request/response pair to a JSON Lines log for later reverse engineering.

Only the Python standard library is used, so it runs anywhere Python 3.9+ is
available (Raspberry Pi, NAS, Docker, ...).

Configuration (environment variables or CLI flags, flags win):

  LISTEN_HOST      address to bind              (default 0.0.0.0)
  LISTEN_PORT      port to bind                 (default 80)
  UPSTREAM         where to forward to, e.g. http://203.0.113.10:80
                   Use the provider's real IP if you override DNS, otherwise
                   the proxy would resolve the name back to itself.
  UPSTREAM_TIMEOUT seconds to wait for the provider (default 30)
  LOG_DIR          directory for the logs        (default ./logs)
  MAX_BODY_LOG     max bytes of each body kept in the log, 0 = unlimited
                   (default 0)
  UI_PORT          port of the log download web UI, 0 = off (default 8089)
  UI_USER / UI_PASSWORD  enable HTTP basic auth on the log UI
  MONITOR_HISTORY_HOURS  hours of decoded values kept for the monitor charts
                   (default 48)

Cloud monitoring (optional, switched on/off in the web UI, see cloud_monitor.py):

  ESTYMA_EMAIL, ESTYMA_PASSWORD (or ESTYMA_PASSWORD_B64 / ESTYMA_PASSWORD_FILE)
                   cloud account; without them the feature stays off
  CLOUD_MONITOR    initial state before it is first switched in the UI (default 0)
  CLOUD_INTERVAL   seconds between cloud polls (default 30)
  ESTYMA_DOMAIN / ESTYMA_LANGUAGE  as for compare/cloud_recorder.py

Local server (see README, "Operating modes"):

  MODE             proxy (forward only, default), hybrid (forward, may add
                   local writes to the cloud's reply) or local (answer the
                   controller ourselves)
  WRITES_ENABLED   1 = allow local writes (default 0, read-only)
  ALLOW_POWER_WRITES  1 = also allow switching the boiler on/off (default 0)
  WRITE_TIMEOUT    seconds until an unconfirmed write fails (default 120)
  WRITE_MIN_INTERVAL  minimum seconds between requests per register (default 10)
  LOCAL_FALLBACK   1 = answer locally when the cloud fails (proxy/hybrid)
  CLOUD_MIRROR     1 = in local mode, still forward each report to the cloud
                   in the background and ignore its reply
  CONTROLLER_TZ    time zone of the controller clock (default Europe/Warsaw)
  CLOCK_SYNC_HOURS hours between clock syncs in local mode, 0 = off (default 6)
  POLL_DELAY       DELAY value sent in local replies (default 10)
  STATE_FILE       persisted register state (default LOG_DIR/state.json)
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import protocol as P
from controller import Controller

# Headers that describe a single connection, not the message. They are not
# forwarded as-is (RFC 9110 section 7.6.1).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class ExchangeLog:
    """Appends one JSON object per request/response pair to a daily file."""

    def __init__(self, log_dir: str, max_body: int) -> None:
        self.log_dir = log_dir
        self.max_body = max_body
        self._lock = threading.Lock()
        self.listeners = []  # callables getting every record, e.g. the monitor
        os.makedirs(log_dir, exist_ok=True)

    def _path(self, ts: datetime) -> str:
        return os.path.join(self.log_dir, f"exchanges-{ts:%Y-%m-%d}.jsonl")

    def body_record(self, body: bytes, content_type: str | None) -> dict:
        rec: dict = {"length": len(body)}
        if not body:
            return rec
        kept = body if not self.max_body else body[: self.max_body]
        if len(kept) < len(body):
            rec["truncated"] = True
        try:
            text = kept.decode("utf-8")
            rec["text"] = text
        except UnicodeDecodeError:
            rec["base64"] = base64.b64encode(kept).decode("ascii")
            text = None
        # Decode common structured formats so the log is easy to grep.
        ctype = (content_type or "").split(";")[0].strip().lower()
        if text is not None:
            if ctype == "application/x-www-form-urlencoded":
                rec["form"] = urllib.parse.parse_qsl(text, keep_blank_values=True)
            elif ctype.endswith("json") or text.lstrip().startswith(("{", "[")):
                try:
                    rec["json"] = json.loads(text)
                except ValueError:
                    pass
        return rec

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        ts = datetime.now(timezone.utc)
        with self._lock:
            with open(self._path(ts), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        for listener in self.listeners:
            try:
                listener(record)
            except Exception as exc:
                print(f"listener error: {type(exc).__name__}: {exc}", flush=True)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Proxy handler
# --------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "estyma-proxy"

    # Set by make_server()
    upstream: urllib.parse.SplitResult | None
    upstream_timeout: float
    log: ExchangeLog
    ctl: Controller | None = None
    local_fallback: bool = False
    cloud_mirror: bool = False
    poll_delay: int = 10

    # BaseHTTPRequestHandler dispatches on do_<METHOD>; route all of them here.
    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self.handle_any
        raise AttributeError(name)

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr log
        pass

    # -- request body -------------------------------------------------------

    def read_request_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                size_line = self.rfile.readline()
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    # Consume trailers up to the blank line.
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    # -- main ---------------------------------------------------------------

    def handle_any(self) -> None:
        exchange_id = uuid.uuid4().hex[:12]
        t_start = time.time()
        req_body = self.read_request_body()

        # A device configured to use an explicit proxy sends absolute URLs.
        target = self.path
        if target.startswith(("http://", "https://")):
            parts = urllib.parse.urlsplit(target)
            target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))

        parsed = urllib.parse.urlsplit(target)
        record: dict = {
            "id": exchange_id,
            "time": iso(t_start),
            "client": f"{self.client_address[0]}:{self.client_address[1]}",
            "request": {
                "method": self.command,
                "target": self.path,
                "path": parsed.path,
                "query": urllib.parse.parse_qsl(parsed.query, keep_blank_values=True),
                "http_version": self.request_version,
                # Keep order and original casing: list of [name, value].
                "headers": [[k, v] for k, v in self.headers.items()],
                "body": self.log.body_record(req_body, self.headers.get("Content-Type")),
            },
        }

        fwd_headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP]
        if req_body or self.headers.get("Content-Length") is not None:
            fwd_headers = [(k, v) for k, v in fwd_headers if k.lower() != "content-length"]
            fwd_headers.append(("Content-Length", str(len(req_body))))

        ctl = self.ctl
        is_connect = ctl is not None and self.command == "POST" and parsed.path == "/device/connect"
        if is_connect:
            try:
                form = P.loose_form(req_body.decode("utf-8", "replace"))
                ctl.observe_report(P.parse_report(form.get("ZAPISZ_ODCZYT", "")))
            except Exception as exc:  # never break the exchange on our side
                print(f"report handling error: {type(exc).__name__}: {exc}", flush=True)
        record["mode"] = ctl.mode if ctl else "proxy"

        result = None
        if is_connect and ctl.mode == "local":
            result = self.local_response(record)
            if self.cloud_mirror and self.upstream:
                threading.Thread(target=self.mirror, args=(target, fwd_headers, req_body),
                                 daemon=True).start()
        elif self.upstream is None:
            result = (404, "Not Found", [("Content-Type", "text/plain")], b"not found\n", "HTTP/1.1")
        else:
            try:
                result = self.forward(target, fwd_headers, req_body)
            except Exception as exc:  # upstream unreachable, timeout, bad response
                record["error"] = f"{type(exc).__name__}: {exc}"
                if is_connect and self.local_fallback:
                    record["fallback"] = True
                    result = self.local_response(record)
            if result is not None and is_connect and not record.get("fallback"):
                result = self.after_cloud_reply(result, record)

        if result is None:
            t_end = time.time()
            record["duration_ms"] = round((t_end - t_start) * 1000, 1)
            self.log.write(record)
            print(f"{iso(t_end)} {exchange_id} {self.command} {self.path} -> ERROR {record['error']}",
                  flush=True)
            msg = b"upstream error\n"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        status, reason, resp_headers, resp_body, resp_version = result

        t_end = time.time()
        ctype = next((v for k, v in resp_headers if k.lower() == "content-type"), None)
        record["response"] = {
            "time": iso(t_end),
            "status": status,
            "reason": reason,
            "http_version": resp_version,
            "headers": [[k, v] for k, v in resp_headers],
            "body": self.log.body_record(resp_body, ctype),
        }
        record["duration_ms"] = round((t_end - t_start) * 1000, 1)
        self.log.write(record)
        print(f"{iso(t_end)} {exchange_id} {self.command} {self.path} -> {status} "
              f"({len(req_body)}B up, {len(resp_body)}B down, {record['duration_ms']}ms)",
              flush=True)

        # Relay the response. The body was fully read (de-chunked), so send it
        # with an explicit Content-Length.
        self.send_response_only(status, reason)
        for k, v in resp_headers:
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        if self.command != "HEAD":
            self.send_header("Content-Length", str(len(resp_body)))
        else:
            cl = next((v for k, v in resp_headers if k.lower() == "content-length"), None)
            if cl is not None:
                self.send_header("Content-Length", cl)
        self.end_headers()
        if self.command != "HEAD" and resp_body:
            self.wfile.write(resp_body)

    # -- local server -------------------------------------------------------

    def local_response(self, record: dict):
        """Build the whole reply ourselves (local mode or fallback)."""
        ctl = self.ctl
        writes = ctl.next_writes(local_reply=True)
        text = P.build_reply(ctl.read_list(), writes, tz=ctl.tz, delay=self.poll_delay)
        record["local"] = True
        if writes:
            record["writes"] = [f"{r},{i}={v}" for r, i, v in writes]
        headers = [
            ("Date", formatdate(usegmt=True)),
            ("Server", "Apache"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST"),
            ("Access-Control-Allow-Headers", "*"),
            ("Content-Type", "text/html"),
        ]
        return 200, "OK", headers, text.encode("utf-8"), "HTTP/1.1"

    def after_cloud_reply(self, result, record: dict):
        """Proxy/hybrid: learn from the cloud's reply, maybe add a local write."""
        status, reason, headers, body, version = result
        if status != 200:
            return result
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return result
        ctl = self.ctl
        try:
            ctl.observe_cloud_reply(text)
            if ctl.mode == "hybrid" and P.can_inject(text):
                writes = ctl.next_writes(local_reply=False)
                new = P.inject_writes(text, writes) if writes else None
                if new is not None:
                    record["injected"] = [f"{r},{i}={v}" for r, i, v in writes]
                    body = new.encode("utf-8")
        except Exception as exc:
            print(f"reply handling error: {type(exc).__name__}: {exc}", flush=True)
        return status, reason, headers, body, version

    def mirror(self, target: str, headers, body: bytes) -> None:
        """Local mode with CLOUD_MIRROR: forward the report, ignore the reply."""
        t0 = time.time()
        rec: dict = {"id": uuid.uuid4().hex[:12], "time": iso(t0), "mirror": True,
                     "request": {"method": self.command, "path": target}}
        try:
            status, reason, _, resp_body, _ = self.forward(target, headers, body)
            rec["response"] = {"time": iso(time.time()), "status": status, "reason": reason,
                               "body": self.log.body_record(resp_body, "text/html")}
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["duration_ms"] = round((time.time() - t0) * 1000, 1)
        self.log.write(rec)

    def forward(self, target: str, headers, body: bytes):
        up = self.upstream
        port = up.port or (443 if up.scheme == "https" else 80)
        conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(up.hostname, port, timeout=self.upstream_timeout)
        try:
            # skip_host / skip_accept_encoding: send the device's headers verbatim.
            conn.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            conn.endheaders(body if body else None)
            resp = conn.getresponse()
            resp_body = resp.read()
            version = {10: "HTTP/1.0", 11: "HTTP/1.1"}.get(resp.version, str(resp.version))
            return resp.status, resp.reason, resp.getheaders(), resp_body, version
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def make_server(listen_host: str, listen_port: int, upstream: str | None, timeout: float,
                log: ExchangeLog, ctl: Controller | None = None, local_fallback: bool = False,
                cloud_mirror: bool = False, poll_delay: int = 10) -> ThreadingHTTPServer:
    up = None
    if upstream:
        up = urllib.parse.urlsplit(upstream if "://" in upstream else f"http://{upstream}")
        if not up.hostname:
            raise SystemExit(f"invalid UPSTREAM: {upstream!r}")
    handler = type("Handler", (ProxyHandler,), {
        "upstream": up,
        "upstream_timeout": timeout,
        "log": log,
        "ctl": ctl,
        "local_fallback": local_fallback,
        "cloud_mirror": cloud_mirror,
        "poll_delay": poll_delay,
    })
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    return ThreadingHTTPServer((listen_host, listen_port), handler)


def main(argv=None) -> int:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--listen-host", default=env("LISTEN_HOST", "0.0.0.0"))
    p.add_argument("--listen-port", type=int, default=int(env("LISTEN_PORT", "80")))
    p.add_argument("--upstream", default=env("UPSTREAM"),
                   help="e.g. http://203.0.113.10:80 (provider's real address)")
    p.add_argument("--upstream-timeout", type=float, default=float(env("UPSTREAM_TIMEOUT", "30")))
    p.add_argument("--log-dir", default=env("LOG_DIR", "logs"))
    p.add_argument("--max-body-log", type=int, default=int(env("MAX_BODY_LOG", "0")))
    p.add_argument("--ui-port", type=int, default=int(env("UI_PORT", "8089")),
                   help="port of the log download web UI, 0 = disabled")
    p.add_argument("--ui-user", default=env("UI_USER"))
    p.add_argument("--ui-password", default=env("UI_PASSWORD"))
    p.add_argument("--monitor-history-hours", type=float,
                   default=float(env("MONITOR_HISTORY_HOURS", "48")))
    p.add_argument("--mode", default=env("MODE", "proxy"), choices=["proxy", "hybrid", "local"])
    p.add_argument("--state-file", default=env("STATE_FILE"))
    p.add_argument("--controller-tz", default=env("CONTROLLER_TZ", "Europe/Warsaw"))
    p.add_argument("--clock-sync-hours", type=float, default=float(env("CLOCK_SYNC_HOURS", "6")))
    p.add_argument("--poll-delay", type=int, default=int(env("POLL_DELAY", "10")))
    p.add_argument("--write-timeout", type=float, default=float(env("WRITE_TIMEOUT", "120")))
    p.add_argument("--write-min-interval", type=float,
                   default=float(env("WRITE_MIN_INTERVAL", "10")))
    args = p.parse_args(argv)
    writes_enabled = env_flag("WRITES_ENABLED")
    allow_power = env_flag("ALLOW_POWER_WRITES")
    local_fallback = env_flag("LOCAL_FALLBACK")
    cloud_mirror = env_flag("CLOUD_MIRROR")

    if not args.upstream and (args.mode != "local" or cloud_mirror or local_fallback):
        p.error("UPSTREAM is required (env var or --upstream) unless MODE=local")

    log = ExchangeLog(args.log_dir, args.max_body_log)
    ctl = Controller(args.mode, args.log_dir, writes_enabled=writes_enabled,
                     allow_power_writes=allow_power, write_timeout=args.write_timeout,
                     write_min_interval=args.write_min_interval,
                     clock_sync_interval=args.clock_sync_hours * 3600,
                     tz=load_tz(args.controller_tz), state_file=args.state_file)
    server = make_server(args.listen_host, args.listen_port, args.upstream,
                         args.upstream_timeout, log, ctl, local_fallback=local_fallback,
                         cloud_mirror=cloud_mirror, poll_delay=args.poll_delay)
    where = {"proxy": f"forwarding to {args.upstream}",
             "hybrid": f"forwarding to {args.upstream} (hybrid: local writes added)",
             "local": "answering locally" + (f", mirroring to {args.upstream}" if cloud_mirror else "")}
    print(f"estyma-proxy listening on {args.listen_host}:{args.listen_port}, mode {args.mode}, "
          f"{where[args.mode]}, logging to {os.path.abspath(args.log_dir)}", flush=True)
    print(f"writes {'ENABLED' if writes_enabled else 'off'}"
          f"{', on/off allowed' if writes_enabled and allow_power else ''}"
          f"{', local fallback on' if local_fallback else ''}", flush=True)
    if args.ui_port:
        from log_ui import start_log_ui
        from monitor import Monitor
        mon = Monitor(args.log_dir, args.monitor_history_hours)
        log.listeners.append(mon.ingest)
        mon.start()
        cloud = None
        try:
            from cloud_monitor import CloudMonitor
            cloud = CloudMonitor(args.log_dir)
            cloud.start()
            print(f"cloud monitor: {'configured' if cloud.configured else cloud.problem}, "
                  f"{'on' if cloud.enabled and cloud.configured else 'off'} "
                  f"(switch it in the web UI)", flush=True)
        except Exception as exc:  # never keep the proxy from starting
            print(f"cloud monitor unavailable: {type(exc).__name__}: {exc}", flush=True)
        start_log_ui(args.listen_host, args.ui_port, args.log_dir,
                     args.ui_user, args.ui_password, monitor=mon, controller=ctl, cloud=cloud)
        auth = "basic auth on" if args.ui_user and args.ui_password else "no auth"
        print(f"log UI on http://{args.listen_host}:{args.ui_port}/ ({auth})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctl.save_state(force=True)
    return 0


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def load_tz(name: str):
    """Time zone of the controller clock; fixed UTC+1 if tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:
        from datetime import timedelta
        print(f"time zone {name!r} not available ({exc}), using UTC+1", flush=True)
        return timezone(timedelta(hours=1))


if __name__ == "__main__":
    sys.exit(main())
