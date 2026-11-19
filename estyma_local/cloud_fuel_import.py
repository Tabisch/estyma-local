"""One-time import of the Estyma cloud's fuel statistics into the history.

The cloud keeps its own fuel consumption chart (kg per day, one month at a
time), going back to when the controller was first connected. The local
fuel statistics (fuel_stats.py) only start when this server started
recording, so this importer copies the cloud's days into the history store
(table ``cloud_fuel``), where fuel_stats.py uses them for the days before
the first local reading.

Everything goes through the EstymaApi wrapper (0.0.71 or newer):
``initialize()``, ``getDevices()``, ``getStatisticsLimits()`` for the years
the cloud offers and ``getFuelStatistics(device, year, month)`` per month.
The credentials are the same ``ESTYMA_*`` container variables as cloud
monitoring; the web UI only starts the import. Today is never imported
(the cloud's number is not final yet), nor leading empty days before the
first day with consumption. Importing again replaces the stored days, so
nothing is counted twice.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from calendar import monthrange
from datetime import date

from . import cloud_recorder as CR

PAUSE_S = 0.5           # between two month requests, to be gentle with the cloud
MAX_KG_PER_DAY = 500.0  # anything above is not a real day (the burner does ~70 kg max)
REQUIRED = "getFuelStatistics"


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))   # "1,320.30" like the wrapper
    except ValueError:
        return None


def day_of(name, index: int, year: int, month: int) -> date | None:
    """The date of one point of the cloud's month chart. Its name is the
    day of the month ("1", "01", "01.11", "2026-11-01", ...); without a
    usable name the position in the list is used."""
    text = str(name if name is not None else "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"(\d{1,2})(?:\D|$)", text)
        day = int(m.group(1)) if m else index + 1
        return date(year, month, day)
    except ValueError:
        return None


def month_days(result: dict, year: int, month: int) -> dict:
    """{date: kg} from one getFuelStatistics() result."""
    out = {}
    points = (result or {}).get("data") or []
    if len(points) > monthrange(year, month)[1]:
        return out
    for i, p in enumerate(points):
        d = day_of(p.get("day"), i, year, month)
        kg = _num(p.get("fuel"))
        if d is None or kg is None or not (0 <= kg <= MAX_KG_PER_DAY):
            continue
        if d.year == year and d.month == month:
            out[d] = kg
    return out


def trim(days: dict, today: date) -> dict:
    """Drop today and later, and the empty days before the first use."""
    keep = sorted((d, kg) for d, kg in days.items() if d < today)
    while keep and keep[0][1] <= 0:
        keep.pop(0)
    return dict(keep)


class CloudFuelImport:
    def __init__(self, store, fuel=None, environ=None, api_factory=None,
                 today=None, pause: float = PAUSE_S) -> None:
        env = os.environ if environ is None else environ
        self.store = store
        self.fuel = fuel
        self.domain = env.get("ESTYMA_DOMAIN") or "igneo.pl"
        self.language = env.get("ESTYMA_LANGUAGE") or "english"
        self.device = (env.get("ESTYMA_DEVICE_ID") or "").strip() or None
        self.pause = pause
        self._today = today or (lambda: fuel.today() if fuel is not None else date.today())
        self.problem = None
        try:
            self._email, self._password = CR.env_credentials(env)
        except Exception as exc:
            self._email = self._password = None
            self.problem = f"credentials could not be read: {type(exc).__name__}"
        self.configured = bool(self._email and self._password)
        if api_factory is None and (CR.EstymaApi is None or not hasattr(CR.EstymaApi, REQUIRED)):
            self.configured = False
            self.problem = ("EstymaApiWrapper is not installed" if CR.EstymaApi is None
                            else "EstymaApiWrapper 0.0.71 or newer is needed")
        elif not self.configured and self.problem is None:
            self.problem = "set ESTYMA_EMAIL and ESTYMA_PASSWORD in the container environment"
        self.api_factory = api_factory or self._default_api
        self._lock = threading.Lock()
        self._run = {"state": "idle", "message": None, "started": None, "finished": None,
                     "done": 0, "total": 0, "device": None}
        self._thread = None

    def _default_api(self):
        return CR.EstymaApi(Email=self._email, Password=self._password, Domain=self.domain,
                            scanInterval=0, language=self.language)

    # -- state --------------------------------------------------------------

    def _set(self, **kw) -> None:
        with self._lock:
            self._run.update(kw)

    def status(self) -> dict:
        with self._lock:
            run = dict(self._run)
        return {"configured": self.configured, "problem": self.problem, "domain": self.domain,
                "now": time.time(), **run, "stored": self.store.cloud_fuel_status()}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> dict:
        """Start the import in the background (from the web UI)."""
        if not self.configured:
            raise ValueError(self.problem or "cloud access is not configured")
        with self._lock:
            if self.running:
                raise ValueError("an import is already running")
            self._run.update(state="running", message=None, started=time.time(), finished=None,
                             done=0, total=0, device=None)
            self._thread = threading.Thread(target=lambda: asyncio.run(self._main()),
                                            name="cloud-fuel-import", daemon=True)
            self._thread.start()
        return self.status()

    def clear(self) -> dict:
        if self.running:
            raise ValueError("an import is running")
        n = self.store.clear_cloud_fuel()
        self._set(state="idle", message=f"{n} imported days deleted", finished=time.time())
        return self.status()

    # -- import -------------------------------------------------------------

    async def _main(self) -> None:
        api = self.api_factory()
        try:
            await api.initialize()
            days, device = await self._fetch(api)
            n = self.store.put_cloud_fuel({d.isoformat(): kg for d, kg in days.items()}, device)
            if self.fuel is not None:
                self.fuel.refresh()
            msg = f"{n} days imported" if n else "the cloud has no fuel statistics for this device"
            self._set(state="done", message=msg, finished=time.time())
            print(f"cloud fuel import: {msg}", flush=True)
        except Exception as exc:
            msg = CR.error_chain(exc)
            self._set(state="error", message=msg, finished=time.time())
            print(f"cloud fuel import failed: {msg}", flush=True)
        finally:
            try:
                await api._logout()
            except Exception:
                pass
            await CR.close_api(api)

    async def _pick_device(self, api) -> str:
        devices = await api.getDevices() or {}
        ids = [str(k) for k in devices]
        if self.device is not None:
            if self.device not in ids:
                raise ValueError(f"device {self.device} (ESTYMA_DEVICE_ID) is not in this account")
            return self.device
        if not ids:
            raise ValueError("no device in this cloud account")
        if len(ids) > 1:
            raise ValueError(f"several devices in this account ({', '.join(ids)}): "
                             f"set ESTYMA_DEVICE_ID to the one to import")
        return ids[0]

    async def _fetch(self, api) -> tuple[dict, str]:
        device = await self._pick_device(api)
        self._set(device=device)
        today = self._today()
        limits = (await api.getStatisticsLimits(device)).get("fuelStatistics") or {}
        years = sorted(int(y) for y in (limits.get("years") or [today.year]) if int(y) <= today.year)
        months = [(y, m) for y in years for m in range(1, 13) if (y, m) <= (today.year, today.month)]
        self._set(total=len(months))
        days = {}
        for i, (y, m) in enumerate(months):
            if i and self.pause:
                await asyncio.sleep(self.pause)
            result = await api.getFuelStatistics(device, y, m, validate=False)
            days.update(month_days(result, y, m))
            self._set(done=i + 1)
        return trim(days, today), device
