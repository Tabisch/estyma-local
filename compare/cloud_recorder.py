#!/usr/bin/env python3
"""Record what the Estyma cloud reports, so it can be matched to the proxy logs.

Uses the EstymaApi wrapper (https://github.com/Tabisch/EstymaApi, PyPI package
``EstymaApiWrapper``) to log in to the cloud web UI and poll the device data.
Every poll appends one JSON line per device to ``<out-dir>/cloud-YYYY-MM-DD.jsonl``:

    {
      "time": "2026-11-07T17:10:00.123+00:00",   # when the cloud was asked (UTC)
      "device_id": "4251681784",
      "data_age_s": 12,                          # cloud's "online.diff": age of its data
      "raw":    {"kociol_temp_sub1": 70.1, "online.diff": "0d 0h:0m:12s", ...},
      "values": {"temp_boiler_sub1": 70.1, "status_controller_sub1": 1, ...}
    }

``raw`` is the unmodified ``info_panel_update`` response (Polish field names,
nested objects flattened with dots). ``values`` is what the wrapper returns from
``getDeviceData(textToValues=True)``: English names, select fields as numbers.
The first line written per run has ``"type": "settings"`` and holds the
selectable settings (``getAvailableSettings()``).

Credentials come from the environment or a JSON file, never from the repo:

    ESTYMA_EMAIL=... ESTYMA_PASSWORD=... python3 compare/cloud_recorder.py
    python3 compare/cloud_recorder.py --credentials ~/estyma-credentials.json

The credentials file looks like ``{"email": "...", "password": "..."}`` (the
same format the wrapper's own tests use). If the password starts or ends with
a space and your tooling trims env values, use ``ESTYMA_PASSWORD_B64``
(base64 of the password) or ``ESTYMA_PASSWORD_FILE`` (path to a file holding
it, e.g. a Docker secret) instead. ``ESTYMA_DOMAIN`` / ``--domain``
selects the cloud (default ``igneo.pl``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

try:
    from EstymaApiWrapper import EstymaApi
except ImportError:  # pragma: no cover - only hit when the wrapper is missing
    # The helpers below are also used by the proxy's cloud monitor
    # (proxy/cloud_monitor.py), which reports the missing wrapper itself.
    EstymaApi = None

MISSING_WRAPPER = "EstymaApiWrapper is not installed: pip install -r compare/requirements.txt"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def to_number(value):
    """Turn numeric strings into int/float, leave everything else alone."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return value
    return value


def flatten(obj, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1]] = to_number(obj)
    return out


def env_credentials(environ=None) -> tuple[str | None, str | None]:
    """ESTYMA_EMAIL plus ESTYMA_PASSWORD / _B64 / _FILE; missing parts are None."""
    env = os.environ if environ is None else environ
    email = env.get("ESTYMA_EMAIL") or None
    password = env.get("ESTYMA_PASSWORD") or None
    # Alternatives that survive tools which trim spaces from env values
    # (compose .env files, some UIs): base64, or a file (e.g. a Docker secret).
    if env.get("ESTYMA_PASSWORD_B64"):
        import base64
        password = base64.b64decode(env["ESTYMA_PASSWORD_B64"].strip()).decode("utf-8")
    if env.get("ESTYMA_PASSWORD_FILE"):
        with open(env["ESTYMA_PASSWORD_FILE"], encoding="utf-8") as fh:
            password = fh.read().rstrip("\r\n")
    return email, password


def load_credentials(args) -> tuple[str, str]:
    email, password = env_credentials()
    if args.credentials:
        with open(os.path.expanduser(args.credentials), encoding="utf-8") as fh:
            cred = json.load(fh)
        email, password = cred["email"], cred["password"]
    if not email or not password:
        msg = "set ESTYMA_EMAIL and ESTYMA_PASSWORD, or pass --credentials FILE"
        if os.environ.get("IDLE_IF_UNCONFIGURED"):
            # In the compose stack the recorder always starts. Without
            # credentials it waits instead of exiting, so restart policies
            # don't turn it into a crash loop.
            print(f"cloud recorder disabled: {msg}. Idling.", flush=True)
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            while True:
                time.sleep(3600)
        sys.exit(msg)
    return email, password


class Recorder:
    def __init__(self, api: EstymaApi, out_dir: str) -> None:
        self.api = api
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)

    def write(self, record: dict) -> None:
        path = os.path.join(self.out_dir, f"cloud-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def fetch_raw(self, device_id: str) -> dict:
        """Same request the wrapper makes, but without its post-processing."""
        api = self.api
        resp = await api._makeRequest(
            "post",
            api.update_url.format(api._http_url),
            data=api.fetchDevicedataBody.format(device_id),
        )
        return await resp.json(content_type=None)

    async def poll_once(self, device_filter: set[str] | None) -> list[dict]:
        """Poll every device once, write and return the records."""
        t = time.time()
        records = []
        try:
            values_all = await self.api.getDeviceData(textToValues=True)
        except Exception as exc:  # keep recording even if one poll fails
            values_all = {}
            print(f"{iso(t)} getDeviceData failed: {type(exc).__name__}: {exc}", flush=True)

        for device_id in list((self.api.devices or {}).keys()):
            if device_filter and device_id not in device_filter:
                continue
            record: dict = {"time": iso(t), "device_id": device_id}
            try:
                raw = await self.fetch_raw(device_id)
                record["raw"] = flatten(raw)
            except Exception as exc:
                record["error"] = f"raw: {type(exc).__name__}: {exc}"
                raw = None
            values = values_all.get(device_id) if isinstance(values_all, dict) else None
            if values:
                record["values"] = flatten(values)
                record["data_age_s"] = values.get("online", {}).get("diffSeconds")
            elif raw is not None:
                diff = raw.get("online", {}).get("diff")
                if diff:
                    try:
                        record["data_age_s"] = await self.api._calculateUpdateDiffSeconds(diff)
                    except Exception:
                        pass
            self.write(record)
            records.append(record)
            n = len(record.get("raw", {}))
            print(f"{record['time']} device {device_id}: {n} raw fields, "
                  f"data age {record.get('data_age_s')} s", flush=True)
        return records


def describe_secret(name: str, value: str) -> str:
    """Hints about a credential without revealing it."""
    notes = [f"{len(value)} chars"]
    if value[:1].isspace():
        notes.append("starts with whitespace")
    if value[-1:].isspace():
        notes.append("ends with whitespace")
    if value[:1] in "\"'" and value[-1:] == value[:1]:
        notes.append("is wrapped in quotes, which are sent as part of the value")
    if "$" in value:
        notes.append("contains '$' (in a compose .env file write it as '$$')")
    return f"{name}: " + ", ".join(notes)


def error_chain(exc: BaseException) -> str:
    """The wrapper re-raises a bare Exception; show what was underneath."""
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = f"{type(exc).__name__}: {exc}".rstrip(": ")
        if text != "Exception":
            parts.append(text)
        exc = exc.__cause__ or exc.__context__
    return " <- ".join(parts) or "no detail from the wrapper"


async def diagnose_login(domain: str, email: str, password: str) -> str:
    """Repeat the wrapper's login request and explain the answer."""
    import re
    import urllib.parse

    import aiohttp

    url = EstymaApi.login_url.format(domain)
    body = EstymaApi.loginDataBody.format(urllib.parse.quote(email), urllib.parse.quote(password))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=body, headers=EstymaApi.headers,
                                    allow_redirects=False, ssl=False,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                status = resp.status
                location = resp.headers.get("Location", "")
                text = await resp.text(errors="replace")
    except Exception as exc:
        return (f"could not reach {url} ({type(exc).__name__}: {exc}). Check DNS and "
                f"outbound HTTPS from this container")

    if status == 302:
        return (f"the login itself works (302 -> {location or '?'}); a later setup "
                f"step failed")
    if 500 <= status < 600:
        return f"the cloud answered {status}; it is probably down or overloaded, retrying"
    if status == 200:
        hint = ""
        m = re.search(r'class="[^"]*(?:error|alert|warning)[^"]*"[^>]*>\s*([^<]{3,200})', text, re.I)
        if m:
            hint = f' The page says: "{m.group(1).strip()}".'
        return (f"the cloud rejected the login (200 with the login page instead of a "
                f"302 redirect). Most likely ESTYMA_EMAIL or ESTYMA_PASSWORD is wrong, or "
                f"the account belongs to a different domain (ESTYMA_DOMAIN={domain}).{hint}")
    return f"unexpected answer {status} from {url}" + (f" -> {location}" if location else "")


async def close_api(api) -> None:
    session = getattr(api, "_session", None)
    if session is not None and not session.closed:
        await session.close()


async def login(args, email: str, password: str, stop: asyncio.Event):
    """Log in, retrying with backoff instead of crash-looping the container."""
    print(f"logging in to {args.domain} as {email[:2]}***. Check that these match what "
          f"you expect: "
          f"{describe_secret('email', email)}; {describe_secret('password', password)}",
          flush=True)
    delay = 30.0
    while not stop.is_set():
        api = EstymaApi(Email=email, Password=password, Domain=args.domain,
                        scanInterval=0, language=args.language)
        try:
            await api.initialize()
            return api
        except Exception as exc:
            await close_api(api)
            reason = await diagnose_login(args.domain, email, password)
            print(f"login failed: {reason}. Wrapper error: {error_chain(exc)}. "
                  f"Retrying in {delay:.0f} s.", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 900.0)
    return None


async def run(args) -> int:
    if EstymaApi is None:
        sys.exit(MISSING_WRAPPER)
    email, password = load_credentials(args)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    api = await login(args, email, password, stop)
    if api is None:
        return 0
    rec = Recorder(api, args.out_dir)

    devices = await api.getDevices()
    print(f"logged in to {args.domain}, devices: "
          + ", ".join(f"{k} ({v.get('name')})" for k, v in devices.items()), flush=True)
    device_filter = set(args.device) if args.device else None

    try:
        rec.write({"type": "settings", "time": iso(time.time()),
                   "devices": devices, "settings": await api.getAvailableSettings()})
    except Exception as exc:
        print(f"could not record settings: {exc}", flush=True)

    count = 0
    try:
        while not stop.is_set():
            started = time.monotonic()
            await rec.poll_once(device_filter)
            count += 1
            if args.count and count >= args.count:
                break
            wait = max(0.0, args.interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await api._logout()
        finally:
            await close_api(api)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--credentials", help='JSON file {"email": ..., "password": ...}')
    p.add_argument("--domain", default=os.environ.get("ESTYMA_DOMAIN", "igneo.pl"))
    p.add_argument("--language", default=os.environ.get("ESTYMA_LANGUAGE", "english"))
    p.add_argument("--interval", type=float, default=float(os.environ.get("INTERVAL", 30)),
                   help="seconds between polls (default 30)")
    p.add_argument("--count", type=int, default=0, help="stop after N polls (default: run forever)")
    p.add_argument("--device", action="append", help="only record this device id (repeatable)")
    p.add_argument("--out-dir", default=os.environ.get("LOG_DIR", "logs"))
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
