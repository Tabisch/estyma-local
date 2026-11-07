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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Proxy handler
# --------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "estyma-proxy"

    # Set by make_server()
    upstream: urllib.parse.SplitResult
    upstream_timeout: float
    log: ExchangeLog

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

        try:
            status, reason, resp_headers, resp_body, resp_version = self.forward(
                target, fwd_headers, req_body
            )
        except Exception as exc:  # upstream unreachable, timeout, bad response
            t_end = time.time()
            record["error"] = f"{type(exc).__name__}: {exc}"
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

def make_server(listen_host: str, listen_port: int, upstream: str, timeout: float,
                log: ExchangeLog) -> ThreadingHTTPServer:
    up = urllib.parse.urlsplit(upstream if "://" in upstream else f"http://{upstream}")
    if not up.hostname:
        raise SystemExit(f"invalid UPSTREAM: {upstream!r}")
    handler = type("Handler", (ProxyHandler,), {
        "upstream": up,
        "upstream_timeout": timeout,
        "log": log,
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
    args = p.parse_args(argv)

    if not args.upstream:
        p.error("UPSTREAM is required (env var or --upstream)")

    log = ExchangeLog(args.log_dir, args.max_body_log)
    server = make_server(args.listen_host, args.listen_port, args.upstream,
                         args.upstream_timeout, log)
    print(f"estyma-proxy listening on {args.listen_host}:{args.listen_port}, "
          f"forwarding to {args.upstream}, logging to {os.path.abspath(args.log_dir)}",
          flush=True)
    if args.ui_port:
        from log_ui import start_log_ui
        start_log_ui(args.listen_host, args.ui_port, args.log_dir,
                     args.ui_user, args.ui_password)
        auth = "basic auth on" if args.ui_user and args.ui_password else "no auth"
        print(f"log UI on http://{args.listen_host}:{args.ui_port}/ ({auth})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
