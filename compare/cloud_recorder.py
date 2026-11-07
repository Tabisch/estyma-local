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
same format the wrapper's own tests use). ``ESTYMA_DOMAIN`` / ``--domain``
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
    sys.exit("EstymaApiWrapper is not installed: pip install -r compare/requirements.txt")


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


def load_credentials(args) -> tuple[str, str]:
    email = os.environ.get("ESTYMA_EMAIL")
    password = os.environ.get("ESTYMA_PASSWORD")
    if args.credentials:
        with open(os.path.expanduser(args.credentials), encoding="utf-8") as fh:
            cred = json.load(fh)
        email, password = cred["email"], cred["password"]
    if not email or not password:
        sys.exit("set ESTYMA_EMAIL and ESTYMA_PASSWORD, or pass --credentials FILE")
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

    async def poll_once(self, device_filter: set[str] | None) -> None:
        t = time.time()
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
            n = len(record.get("raw", {}))
            print(f"{record['time']} device {device_id}: {n} raw fields, "
                  f"data age {record.get('data_age_s')} s", flush=True)


async def run(args) -> int:
    email, password = load_credentials(args)
    api = EstymaApi(Email=email, Password=password, Domain=args.domain,
                    scanInterval=0, language=args.language)
    await api.initialize()
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

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

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
        await api._logout()
        if api._session and not api._session.closed:
            await api._session.close()
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
