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

from protocol import parse_read_list
from registers import RegisterTable

# --------------------------------------------------------------------------
# Register mapping
# --------------------------------------------------------------------------
# decode: "d10" value/10, "d100" value/100, "x1" as is, "low" low byte of a
# 0x1nn flag register (256 + value). lo/hi: plausible range of the decoded
# value; anything outside is treated as a corrupted CAN read and dropped.

# key, label, register, index, decode, unit, lo, hi
LIVE = [
    ("boiler", "Boiler", 8193, 1, "d10", "°C", -20, 120),
    ("buffer_top", "Buffer top", 8466, 1, "d10", "°C", -20, 120),
    ("buffer_bottom", "Buffer bottom", 8467, 1, "d10", "°C", -20, 120),
    ("circuit1", "Heating circuit 1", 16385, 1, "d10", "°C", -20, 120),
    ("outside", "Outside", 8543, 1, "d10", "°C", -50, 60),
    ("flue", "Flue gas", 8473, 1, "low", "°C", 0, 255),
    ("o2", "Oxygen (flue)", 8480, 1, "d100", "%", 0, 25),
    ("power", "Burner power", 16389, 1, "x1", "%", 0, 100),
    ("lambda_pwm", "Lambda PWM", 16391, 1, "low", "", 0, 255),
    ("burner_state", "Burner state", 8199, 1, "low", "", 0, 30),
    ("boiler_pump", "Boiler pump", 8823, 1, "low", "", 0, 1),
    ("boiler_calc", "Boiler target (calculated)", 8196, 1, "x1", "°C", 0, 120),
    ("fuel_counter", "Fuel counter", 8484, 1, "d10", "kg", 0, 2**31 - 1),
]

SETTINGS = [
    ("boiler_set", "Boiler target", 8195, "x1", "°C", 0, 120),
    ("buffer_top_set", "Buffer top target", 8468, "x1", "°C", 0, 120),
    ("buffer_bottom_set", "Buffer bottom target", 8469, "x1", "°C", 0, 120),
    ("room_comfort", "Room comfort, circuit 1", 8242, "d10", "°C", 0, 40),
    ("room_eco", "Room eco, circuit 1", 8264, "d10", "°C", 0, 40),
]
SINGLE_SETTINGS = [
    ("boiler_mode", "Boiler mode", 8194, 1, "x1", "", 0, 255),
    ("circuit1_program", "Circuit 1 program (probable)", 8292, 1, "low", "", 0, 255),
]

# Values kept in the history (for the charts); the rest is "current" only.
HISTORY_KEYS = ["boiler", "buffer_top", "buffer_bottom", "circuit1", "outside",
                "flue", "o2", "power", "burner_state", "boiler_pump", "boiler_calc"]


def decode(raw: int, how: str, lo: float, hi: float):
    if how == "low":
        if raw >> 8 not in (0, 1):  # flag registers are 0x0nn or 0x1nn
            return None
        val = raw & 0xFF
    elif how == "d10":
        val = raw / 10
    elif how == "d100":
        val = raw / 100
    else:
        val = raw
    return val if lo <= val <= hi else None


def loose_form(text: str) -> dict:
    """Split the NUL-padded, not URL-encoded form body of the controller."""
    out = {}
    for part in text.rstrip("\0").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
    return out


def parse_registers(value: str) -> dict:
    regs = {}
    for item in value.split(";"):
        bits = item.strip().split(",")
        if len(bits) != 3:
            continue
        try:
            regs[(int(bits[0]), int(bits[1]))] = int(bits[2])
        except ValueError:
            pass
    return regs


# Alarm ring buffer (see docs/register-mapping.md): slot k has its code and
# state in 16400+k (state in bits 16+, code in the low byte), start time in
# 16420+k and end time in 16440+k (controller clock, 0 while active). The
# counter 16460,1 (0x100 + n) points at slot (n - 1) % 20.
ALARM_SLOTS = 20
ALARM_CODE, ALARM_START, ALARM_END, ALARM_COUNTER = 16400, 16420, 16440, 16460
ALARM_STATES = {0: "cleared", 3: "active", 7: "resetting"}
MIN_TIME = 1_500_000_000  # anything older is a glitch


def decode_alarms(regs: dict, now: float) -> tuple[dict, int | None]:
    """Slot -> partial {code, state, start, end} from one report; plus the counter."""
    slots: dict = {}
    for (reg, idx), raw in regs.items():
        if idx != 1 or not ALARM_CODE <= reg < ALARM_END + ALARM_SLOTS:
            continue
        k = (reg - ALARM_CODE) % ALARM_SLOTS
        if reg < ALARM_START:
            state = (raw >> 16) & 0xFFFF
            if raw < 0 or state not in ALARM_STATES:
                continue
            slots.setdefault(k, {}).update(code=raw & 0xFF, state=ALARM_STATES[state])
        else:
            key = "start" if reg < ALARM_END else "end"
            if raw == 0 and key == "end":
                slots.setdefault(k, {})[key] = 0
            elif MIN_TIME <= raw <= now + 2 * 86400:
                slots.setdefault(k, {})[key] = raw
    counter = None
    raw = regs.get((ALARM_COUNTER, 1))
    if raw is not None and raw >> 8 in (0, 1):
        counter = raw & 0xFF
    return slots, counter


def parse_time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def decode_registers(regs: dict) -> tuple[dict, dict]:
    live = {}
    for key, _, reg, idx, how, _, lo, hi in LIVE:
        if (reg, idx) in regs:
            v = decode(regs[(reg, idx)], how, lo, hi)
            if v is not None:
                live[key] = v
    for key, _, reg, idx, how, _, lo, hi in SINGLE_SETTINGS:
        if (reg, idx) in regs:
            v = decode(regs[(reg, idx)], how, lo, hi)
            if v is not None:
                live[key] = v
    settings = {}
    for key, _, reg, how, _, lo, hi in SETTINGS:
        triple = [decode(regs[(reg, i)], how, lo, hi) if (reg, i) in regs else None
                  for i in (1, 3, 4)]
        if any(v is not None for v in triple):
            settings[key] = triple
    return live, settings


def meta():
    """Labels and units for the page."""
    fields = {k: {"label": l, "unit": u} for k, l, _, _, _, u, _, _ in LIVE + SINGLE_SETTINGS}
    fields.update({k: {"label": l, "unit": u} for k, l, _, _, u, _, _ in SETTINGS})
    return fields


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

        regs = parse_registers(form.get("ZAPISZ_ODCZYT", ""))
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
