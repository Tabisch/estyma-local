"""Live monitor: decodes the controller's reports from the proxy traffic.

Every exchange the proxy logs is also handed to ``Monitor.ingest``. The
``ZAPISZ_ODCZYT`` field of the request ("reg,idx,value;...") is decoded into
named values (temperatures, burner power, O2, ...) and kept in memory for
``MONITOR_HISTORY_HOURS``. On start the history is refilled from the newest
``exchanges-*.jsonl`` files, so a restart does not lose the charts.

The web UI (log_ui.py) serves the page and ``/api/monitor``.
"""

from __future__ import annotations

import glob
import json
import os
import bisect
import threading
import time
from collections import deque
from datetime import datetime

import catalog as C
import decode as D
from protocol import loose_form, parse_read_list, parse_report
from registers import RegisterTable

# --------------------------------------------------------------------------
# Register mapping
# --------------------------------------------------------------------------
# Labels, units, decoding and the plausible range of each value come from
# catalog.py; decode.py drops corrupted reads (see there).


def _field(key, reg):
    """(key, label, register, unit) from the catalog."""
    r = C.get(reg)
    return (key, r.label, reg, r.unit)


LIVE = [
    _field("boiler", 8193),
    _field("buffer_top", 8466),
    _field("buffer_bottom", 8467),
    _field("circuit1", 16385),
    _field("outside", 8543),
    _field("flue", 8473),
    _field("o2", 8480),
    _field("power", 16389),
    _field("lambda_pwm", 16391),
    _field("burner_state", 8199),
    _field("boiler_pump", 8823),
    _field("boiler_calc", 8196),
    _field("fuel_counter", 8484),
]
# Settings shown with their min/max (index 1 value, 3 min, 4 max).
SETTINGS = [
    _field("boiler_set", 8195),
    _field("buffer_top_set", 8468),
    _field("buffer_bottom_set", 8469),
    _field("room_comfort", 8242),
    _field("room_eco", 8264),
]
SINGLE_SETTINGS = [
    _field("boiler_mode", 8194),
    _field("circuit1_program", 8292),
]
KEY_REG = {k: reg for k, _, reg, _ in LIVE + SINGLE_SETTINGS + SETTINGS}

# Values kept in the history (for the charts); the rest is "current" only.
HISTORY_KEYS = ["boiler", "buffer_top", "buffer_bottom", "circuit1", "outside",
                "flue", "o2", "power", "burner_state", "boiler_pump", "boiler_calc"]


# Alarm ring buffer (see catalog.py): the controller keeps the last 20 alarms.
# The counter 16460,1 points at the newest slot, (n - 1) % 20; the next slot,
# n % 20, holds the oldest alarm (display "Num 20").
def decode_alarms(regs: dict, now: float) -> tuple[dict, int | None]:
    """Slot -> partial {code, state, start, end} from one report; plus the counter."""
    slots: dict = {}
    for (reg, idx), raw in regs.items():
        if idx != 1 or not C.ALARM_CODE <= reg < C.ALARM_END + C.ALARM_SLOTS:
            continue
        k = (reg - C.ALARM_CODE) % C.ALARM_SLOTS
        if reg < C.ALARM_START:
            entry = D.alarm_code(raw)
            if entry is not None:
                slots.setdefault(k, {}).update(code=entry[0], state=entry[1])
        else:
            t = D.alarm_time(raw, now)
            key = "start" if reg < C.ALARM_END else "end"
            if t is not None and (t or key == "end"):
                slots.setdefault(k, {})[key] = t
    counter = D.alarm_counter(regs.get((C.ALARM_COUNTER, 1)))
    return slots, counter


def parse_time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def decode_registers(regs: dict) -> tuple[dict, dict]:
    """One report -> (live values, settings [value, min, max]); corrupted
    reads are left out (decode.value)."""
    live = {}
    for key, _, reg, _ in LIVE + SINGLE_SETTINGS:
        v = D.value(reg, regs.get((reg, 1)))
        if v is not None:
            live[key] = v
    settings = {}
    for key, _, reg, _ in SETTINGS:
        triple = [D.value(reg, regs.get((reg, i)), i) for i in (1, 3, 4)]
        if any(v is not None for v in triple):
            settings[key] = triple
    return live, settings


def meta():
    """Labels and units for the page."""
    return {k: {"label": l, "unit": u} for k, l, _, u in LIVE + SINGLE_SETTINGS + SETTINGS}


# --------------------------------------------------------------------------
# Monitor state
# --------------------------------------------------------------------------

class Monitor:
    def __init__(self, log_dir: str, history_hours: float = 48) -> None:
        self.log_dir = log_dir
        self.history_s = history_hours * 3600
        self._lock = threading.Lock()
        self.history: list = []                 # (ts, {key: value}), sorted by ts
        self.current: dict = {}                 # key -> (ts, value)
        self.settings: dict = {}                # key -> (ts, [value, min, max])
        self.device: dict = {}
        self.last_exchange: dict | None = None
        self.last_write: dict | None = None     # last ZAPIS block from the server
        self.alarms: dict = {}                  # slot -> {code, state, start, end, seen}
        self.alarm_counter: tuple | None = None  # (ts, n)
        self.recent: deque = deque()            # (ts, ok) for error stats
        self.loading = False
        self.registers = RegisterTable(log_dir)  # raw view of every register seen

    # -- feeding ------------------------------------------------------------

    def ingest(self, rec: dict, live: bool = True) -> None:
        try:
            self._ingest(rec, live)
        except Exception:  # never break the proxy because of a bad record
            pass

    def _ingest(self, rec: dict, live_rec: bool) -> None:
        req = rec.get("request") or {}
        if req.get("path") != "/device/connect" or rec.get("mirror"):
            return
        ts = parse_time(rec["time"])
        body = req.get("body") or {}
        form = loose_form(body["text"]) if "text" in body else dict(body.get("form") or [])
        resp = rec.get("response")
        status = resp.get("status") if resp else None
        resp_text = ((resp or {}).get("body") or {}).get("text", "")

        regs = parse_report(form.get("ZAPISZ_ODCZYT", ""))
        live, settings = decode_registers(regs)
        self.registers.observe(ts, regs, parse_read_list(resp_text) if resp_text else None)

        with self._lock:
            if live_rec:  # error stats count traffic since start only
                self.recent.append((ts, status == 200))
            # History reload and live traffic run at the same time, so only
            # let newer records replace the "current" view.
            newest = self.last_exchange is None or ts >= self.last_exchange["time"]
            if newest:
                self.last_exchange = {
                    "time": ts,
                    "status": status,
                    "error": rec.get("error"),
                    "duration_ms": rec.get("duration_ms"),
                    "registers": len(regs),
                    "server_info": _between(resp_text, "SERVER_INFO=", "<br>"),
                    "mode": rec.get("mode", "proxy"),
                    "local": bool(rec.get("local")),
                }
                if form.get("SOFT_VER") is not None:
                    self.device = {
                        "firmware": form.get("SOFT_VER", "").strip(),
                        "frame": form.get("NUMER RAMKI", "").strip(),
                        "can_status": form.get("CAN STATUS", "").strip(),
                    }
            if "ZAPIS&" in resp_text and (self.last_write is None or ts >= self.last_write["time"]):
                self.last_write = {"time": ts, "values": _between(resp_text, "ZAPIS&;", "&KONIEC"),
                                   "source": "local" if rec.get("local") or rec.get("injected")
                                   else "cloud"}
            slots, counter = decode_alarms(regs, ts)
            if counter is not None and (self.alarm_counter is None or ts >= self.alarm_counter[0]):
                self.alarm_counter = (ts, counter)
            for k, part in slots.items():
                cur = self.alarms.setdefault(k, {"seen": ts})
                if ts >= cur["seen"] or len(cur) == 1:
                    # A new alarm in a reused slot: forget the old one's fields.
                    if part.get("start") and cur.get("start") and part["start"] != cur["start"]:
                        cur.clear()
                    cur.update(part)
                    cur["seen"] = ts
            for k, v in live.items():
                if k not in self.current or ts >= self.current[k][0]:
                    self.current[k] = (ts, v)
            for k, v in settings.items():
                if k not in self.settings or ts >= self.settings[k][0]:
                    self.settings[k] = (ts, v)
            if live:
                point = (ts, {k: live[k] for k in HISTORY_KEYS if k in live})
                if not self.history or ts >= self.history[-1][0]:
                    self.history.append(point)
                else:
                    bisect.insort(self.history, point, key=lambda p: p[0])
            self._trim(time.time())

    def _trim(self, now: float) -> None:
        cut = bisect.bisect_left(self.history, now - self.history_s, key=lambda p: p[0])
        if cut:
            del self.history[:cut]
        while self.recent and self.recent[0][0] < now - 3600:
            self.recent.popleft()

    def load_history(self) -> None:
        """Refill from the newest log files (blocking; run in a thread)."""
        self.loading = True
        try:
            cutoff = time.time() - self.history_s
            files = sorted(glob.glob(os.path.join(self.log_dir, "exchanges-*.jsonl")))
            days = int(self.history_s // 86400) + 2
            for path in files[-days:]:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if '"/device/connect"' not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        try:
                            if parse_time(rec["time"]) < cutoff:
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                        self.ingest(rec, live=False)
        except OSError:
            pass
        finally:
            self.loading = False
            self.registers.save(force=True)

    def start(self) -> None:
        threading.Thread(target=self.load_history, name="monitor-load", daemon=True).start()

    # -- reading ------------------------------------------------------------

    def _alarm_list(self) -> dict:
        """Known alarm slots, newest first. Times are the controller clock
        (local wall time encoded as Unix time). Call with the lock held."""
        items = [dict(v, slot=k) for k, v in self.alarms.items() if "code" in v]
        items.sort(key=lambda a: (a.get("start") or 0, a["seen"]), reverse=True)
        active = any(a["state"] in ("active", "resetting") and not a.get("end") for a in items)
        burner = self.current.get("burner_state")
        if burner and burner[1] == 10 and time.time() - burner[0] < 300:
            active = True
        return {"active": active, "counter": self.alarm_counter[1] if self.alarm_counter else None,
                "items": items}

    def snapshot(self, hours: float = 24, max_points: int = 1500) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)
            since = now - hours * 3600
            hist = [(t, p) for t, p in self.history if t >= since]
            recent = list(self.recent)
            out = {
                "now": now,
                "loading": self.loading,
                "current": {k: {"time": t, "value": v} for k, (t, v) in self.current.items()},
                "settings": {k: {"time": t, "value": v[0], "min": v[1], "max": v[2]}
                             for k, (t, v) in self.settings.items()},
                "device": dict(self.device),
                "last_exchange": self.last_exchange,
                "last_write": self.last_write,
                "alarms": self._alarm_list(),
            }
        # Downsample evenly so the page stays light over long ranges.
        if len(hist) > max_points:
            step = len(hist) / max_points
            hist = [hist[int(i * step)] for i in range(max_points)] + [hist[-1]]
        out["history"] = {"t": [round(t) for t, _ in hist]}
        for k in HISTORY_KEYS:
            out["history"][k] = [p.get(k) for _, p in hist]
        out["last_hour"] = {"exchanges": len(recent), "ok": sum(1 for _, ok in recent if ok)}
        out["fields"] = meta()
        return out


def _between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    j = text.find(end, i)
    return text[i:j if j >= 0 else None].strip()
