"""Optional cloud monitoring inside the proxy, switched on and off in the web UI.

All cloud access goes through the EstymaApi wrapper
(https://github.com/Tabisch/EstymaApi): ``initialize()`` to log in,
``getDevices()``, ``getAvailableSettings()`` and
``getDeviceData(textToValues=True)`` to poll. The wrapper handles the session
and re-logins itself. Each poll appends a ``cloud-YYYY-MM-DD.jsonl`` record
with the wrapper's values (the ``values`` part of compare/cloud_recorder.py's
format, which compare/compare_cloud_proxy.py reads) to LOG_DIR. The latest
values are kept in memory for the monitor page.

Credentials only ever come from the container environment (ESTYMA_EMAIL plus
ESTYMA_PASSWORD, ESTYMA_PASSWORD_B64 or ESTYMA_PASSWORD_FILE). The web UI can
only switch polling on or off; it never sees or stores the credentials.
Without credentials the feature shows as "not configured" and stays off.

The on/off choice is saved in LOG_DIR/cloud-monitor.json so it survives
restarts. Before the first switch, CLOUD_MONITOR (default 0) decides.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

_here = os.path.dirname(os.path.abspath(__file__))
# compare/ sits next to this file in the image (/app/compare) and next to
# proxy/ in the repository.
for _p in (os.path.join(_here, "compare"), os.path.join(os.path.dirname(_here), "compare")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import cloud_recorder as CR  # noqa: E402

# Wrapper fields shown on the monitor page (the wrapper's English names, see
# docs/cloud-translation.md), keyed like the monitor's own fields so the page
# can use the same labels and compare them with the local values.
SHOWN_FIELDS = {
    "boiler": "temp_boiler_sub1",
    "boiler_set": "temp_boiler_target_sub1",
    "buffer_top": "temp_buffer_top_sub1",
    "buffer_bottom": "temp_buffer_bottom_sub1",
    "circuit1": "temp_heating_curcuit1_sub1",
    "outside": "temp_outside_sub1",
    "flue": "temp_exhaust_boiler_sub1",
    "burner_state": "current_status_burner_sub1_int",
}
MAX_FAILURES = 5  # failed polls in a row before logging in again


class CloudMonitor:
    def __init__(self, log_dir: str, environ=None, interval: float | None = None,
                 api_factory=None) -> None:
        env = os.environ if environ is None else environ
        self.log_dir = log_dir
        self.state_path = os.path.join(log_dir, "cloud-monitor.json")
        self.domain = env.get("ESTYMA_DOMAIN") or "igneo.pl"
        self.language = env.get("ESTYMA_LANGUAGE") or "english"
        self.interval = float(interval if interval is not None else env.get("CLOUD_INTERVAL") or 30)
        self.problem = None  # why the feature cannot run, shown in the UI
        try:
            self._email, self._password = CR.env_credentials(env)
        except Exception as exc:  # unreadable ESTYMA_PASSWORD_FILE, bad base64
            self._email = self._password = None
            self.problem = f"credentials could not be read: {type(exc).__name__}"
        self.configured = bool(self._email and self._password)
        if api_factory is None and CR.EstymaApi is None:
            self.configured = False
            self.problem = "EstymaApiWrapper is not installed"
        elif not self.configured and self.problem is None:
            self.problem = "set ESTYMA_EMAIL and ESTYMA_PASSWORD in the container environment"
        self.api_factory = api_factory or self._default_api

        default = (env.get("CLOUD_MONITOR") or "").strip().lower() in ("1", "true", "yes", "on")
        self.enabled = self._load_enabled(default)
        self._lock = threading.Lock()
        self._status = {"state": "off", "since": time.time(), "message": None,
                        "last_poll": None, "last_ok": None, "polls": 0, "devices": {}}
        self._loop = None
        self._wake = None
        self._thread = None

    # -- state --------------------------------------------------------------

    def _load_enabled(self, default: bool) -> bool:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return bool(json.load(fh).get("enabled"))
        except (OSError, ValueError, AttributeError):
            return default

    def _save_enabled(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"enabled": self.enabled, "changed": time.time()}, fh)
        os.replace(tmp, self.state_path)

    def _set(self, state: str | None = None, **kw) -> None:
        with self._lock:
            if state is not None and state != self._status["state"]:
                self._status["state"] = state
                self._status["since"] = time.time()
            self._status.update(kw)

    def status(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self._status))
        return {"configured": self.configured, "enabled": self.enabled,
                "problem": self.problem, "domain": self.domain,
                "interval": self.interval, "now": time.time(), **st}

    def set_enabled(self, on: bool) -> dict:
        """Switch polling on or off (from the web UI); persisted."""
        on = bool(on)
        if on and not self.configured:
            raise ValueError(self.problem or "cloud monitoring is not configured")
        if on != self.enabled:
            self.enabled = on
            self._save_enabled()
            print(f"cloud monitor switched {'on' if on else 'off'} from the web UI", flush=True)
            self._kick()
        return self.status()

    # -- background loop ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._wake = asyncio.Event()
            ready.set()
            loop.run_until_complete(self._main())

        self._thread = threading.Thread(target=run, name="cloud-monitor", daemon=True)
        self._thread.start()
        ready.wait(5)

    def _kick(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def _sleep(self, seconds: float | None) -> None:
        """Wait until the timeout or until the toggle changes."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _default_api(self):
        return CR.EstymaApi(Email=self._email, Password=self._password, Domain=self.domain,
                            scanInterval=0, language=self.language)

    async def _main(self) -> None:
        if not self.configured:
            self._set("unconfigured")
        while True:
            if not (self.enabled and self.configured):
                self._set("off" if self.configured else "unconfigured", message=None)
                await self._sleep(None)
                continue
            api = await self._login()
            if api is None:
                continue
            try:
                await self._poll(api)
            except Exception as exc:
                self._set("error", message=f"{type(exc).__name__}: {exc}")
                print(f"cloud monitor error: {type(exc).__name__}: {exc}", flush=True)
                await self._sleep(self.interval)
            finally:
                try:
                    await api._logout()
                except Exception:
                    pass
                await CR.close_api(api)

    async def _login(self):
        delay = 30.0
        print(f"cloud monitor: logging in to {self.domain}", flush=True)
        while self.enabled:
            self._set("connecting", message=None, retry_in=None)
            api = self.api_factory()
            try:
                await api.initialize()
                return api
            except Exception as exc:
                await CR.close_api(api)
                msg = f"login failed: {CR.error_chain(exc)}"
                print(f"cloud monitor {msg}. Retrying in {delay:.0f} s.", flush=True)
                self._set("error", message=msg, retry_in=delay)
            await self._sleep(delay)
            delay = min(delay * 2, 900.0)
        return None

    async def _poll(self, api) -> None:
        out = CR.Recorder(api, self.log_dir)  # only used to append to cloud-*.jsonl
        devices = await api.getDevices() or {}
        self._set(devices={str(k): {"name": (v or {}).get("name")} for k, v in devices.items()})
        try:
            out.write({"type": "settings", "time": CR.iso(time.time()), "devices": devices,
                       "settings": await api.getAvailableSettings()})
        except Exception as exc:
            print(f"cloud monitor: could not record settings: {exc}", flush=True)
        failures = 0
        while self.enabled:
            started = time.monotonic()
            now = time.time()
            try:
                # The wrapper re-logs in by itself when its session expires.
                data = await api.getDeviceData(textToValues=True) or {}
                error = None
            except Exception as exc:
                data, error = {}, CR.error_chain(exc)
            with self._lock:
                st = self._status
                st["polls"] += 1
                st["last_poll"] = now
                for device_id, values in data.items():
                    values = values if isinstance(values, dict) else {}
                    flat = CR.flatten(values)
                    age = (values.get("online") or {}).get("diffSeconds")
                    record = {"time": CR.iso(now), "device_id": str(device_id),
                              "data_age_s": age, "values": flat}
                    out.write(record)
                    st["devices"].setdefault(str(device_id), {}).update(
                        time=now, data_age_s=age,
                        values={k: flat[f] for k, f in SHOWN_FIELDS.items()
                                if flat.get(f) is not None})
            if data:
                failures = 0
                self._set("running", message=None, last_ok=now)
            else:
                failures += 1
                self._set("error", message=error or "no device data")
                print(f"cloud monitor: poll failed: {error or 'no device data'}", flush=True)
                if failures >= MAX_FAILURES:
                    return  # log in again
            await self._sleep(max(0.0, self.interval - (time.monotonic() - started)))
