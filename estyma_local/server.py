"""The HTTP server the heating controller talks to.

``ProxyHandler`` answers ``/device/connect`` (and anything else the
controller sends): it forwards to the provider's server (``MODE=proxy`` /
``hybrid``), or answers locally (``local``, ``LOCAL_FALLBACK``, or when the
cloud is too slow or fails, see ``cloud_fallback.py``), and writes
every request/response pair to a JSON Lines log (``ExchangeLog``). The
controller state and local writes live in ``controller.py``.

Started by ``app.py`` (``python3 -m estyma_local``); every setting is an
environment variable, see ``config.py`` / docs/configuration.md.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import protocol as P
from .controller import Controller

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
    fallback = None  # cloud_fallback.CloudFallback or None
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
            fb = self.fallback if is_connect else None
            wait = fb.seconds if fb is not None else 0
            try:
                if wait > 0:
                    result = self.forward_or_give_up(target, fwd_headers, req_body,
                                                     exchange_id, t_start, t_start + wait)
                else:
                    result = self.forward(target, fwd_headers, req_body)
            except Exception as exc:  # upstream unreachable, timeout, bad response
                record["error"] = f"{type(exc).__name__}: {exc}"
                if is_connect and (self.local_fallback or wait > 0):
                    record["fallback"] = True
                    if wait > 0:
                        record["fallback_reason"] = "unreachable"
                        fb.answered_locally("unreachable", record["error"])
                    result = self.local_response(record)
            else:
                if wait > 0 and result is None:  # the cloud is still busy
                    record["fallback"] = True
                    record["fallback_reason"] = "slow"
                    fb.answered_locally("slow", f"no cloud reply within {wait} s")
                    result = self.local_response(record)
                elif wait > 0 and result[0] >= 500:
                    record["fallback"] = True
                    record["fallback_reason"] = "cloud_error"
                    record["cloud_status"] = result[0]
                    fb.answered_locally("cloud_error", f"cloud answered {result[0]} {result[1]}")
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
        read_list = ctl.read_list()
        extra = ctl.extra_reads(read_list)
        text = P.build_reply(read_list + extra, writes, tz=ctl.tz, delay=self.poll_delay)
        record["local"] = True
        if extra:
            record["extra_reads"] = len(extra)
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
                    text = new
                    body = new.encode("utf-8")
            if ctl.mode == "hybrid":
                extra = ctl.extra_reads(P.parse_read_list(text) or ())
                new = P.add_reads(text, extra) if extra else None
                if new is not None:
                    record["extra_reads"] = len(extra)
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

    def forward_or_give_up(self, target: str, headers, body: bytes, exchange_id: str,
                           t_start: float, deadline: float):
        """``forward`` with a deadline: the cloud's reply, or None when it has
        not come by ``deadline``. The cloud request then keeps running in the
        background and its late reply is logged as a record of its own."""
        box: dict = {}
        lock = threading.Lock()
        done = threading.Event()

        def run():
            try:
                res, exc = self.forward(target, headers, body), None
            except Exception as e:  # noqa: BLE001 - reported below
                res, exc = None, e
            with lock:
                box["result"], box["exc"] = res, exc
                late = box.get("late", False)
            done.set()
            if late:
                self.log_late(exchange_id, t_start, res, exc)

        threading.Thread(target=run, name=f"cloud-{exchange_id}", daemon=True).start()
        done.wait(max(0.0, deadline - time.time()))
        with lock:
            if "result" not in box:
                box["late"] = True
                return None
        if box["exc"] is not None:
            raise box["exc"]
        return box["result"]

    def log_late(self, exchange_id: str, t_start: float, result, exc) -> None:
        """The cloud's reply to a report we already answered locally."""
        rec: dict = {"id": uuid.uuid4().hex[:12], "time": iso(t_start), "late_for": exchange_id,
                     "request": {"method": self.command, "path": "/device/connect"}}
        if exc is not None:
            rec["error"] = f"{type(exc).__name__}: {exc}"
        else:
            status, reason, headers, resp_body, _ = result
            ctype = next((v for k, v in headers if k.lower() == "content-type"), None)
            rec["response"] = {"time": iso(time.time()), "status": status, "reason": reason,
                               "body": self.log.body_record(resp_body, ctype)}
        rec["duration_ms"] = round((time.time() - t_start) * 1000, 1)
        if self.fallback is not None:
            self.fallback.late_reply()
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
                cloud_mirror: bool = False, poll_delay: int = 10,
                fallback=None) -> ThreadingHTTPServer:
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
        "fallback": fallback,
        "cloud_mirror": cloud_mirror,
        "poll_delay": poll_delay,
    })
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    return ThreadingHTTPServer((listen_host, listen_port), handler)
