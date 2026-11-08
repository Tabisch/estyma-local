"""Local server core: register state, write queue with safety checks.

One ``Controller`` instance owns everything the server knows about the
heating controller:

* the last validated raw value of every reported register (persisted to a
  small JSON state file so restarts keep it),
* the read list (``ODCZYT``) learned from the cloud, plus the alarm slot,
* the queue of local writes (``ZAPIS``) with read-back confirmation, and the
  audit log of every write (``writes-YYYY-MM-DD.jsonl`` next to the logs).

Safety rules (see docs/home-assistant-roadmap.md, "Writes, setpoints and
safety"):

1. Only registers in ``WRITABLE`` can be written, with fixed value ranges
   intersected with the controller's own min/max.
2. Writes are off unless ``WRITES_ENABLED=1``; on/off needs in addition
   ``ALLOW_POWER_WRITES=1``.
3. At most one pending write per register; a newer request replaces it.
   Requests for the same register are rate limited.
4. A write is sent on every poll until the controller reports the new
   value, then dropped. It fails after ``WRITE_TIMEOUT`` seconds.
5. One register per ``ZAPIS`` (plus the apply flag), because multi-register
   writes are not verified yet.
6. While an alarm is active only on/off writes are sent (switching off and
   on is how the alarm is reset at the controller).
7. In hybrid mode the cloud wins: a cloud write to the same register cancels
   the local one, and nothing is injected into a reply that already writes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import protocol as P

# --------------------------------------------------------------------------
# Register model
# --------------------------------------------------------------------------

BURNER_STATE = (8199, 1)
BURNER_STATE_ERROR = 10


def low16(raw: int) -> int:
    """Corrupted CAN reads carry garbage in the upper 16 bits only."""
    return raw & 0xFFFF


def flag_value(raw: int) -> int | None:
    """0x100 | value style registers (256/257) -> value, else None."""
    v = low16(raw)
    return v & 0xFF if v >> 8 == 1 else None


@dataclass(frozen=True)
class Writable:
    key: str
    label: str
    reg: int
    idx: int
    kind: str                 # "int" or "bool"
    lo: int                   # local hard limits (always applied)
    hi: int
    power: bool = False       # needs ALLOW_POWER_WRITES
    limit_regs: tuple | None = None  # (min reg, max reg) reported by the controller
    unit: str = ""

    def readback(self, raw: int | None) -> int | None:
        if raw is None:
            return None
        if self.kind == "bool":
            return flag_value(raw)
        return low16(raw)


# Only registers whose writes were seen working from the cloud (2026-11-08).
WRITABLE = {
    w.key: w for w in [
        Writable("boiler_target", "Boiler target", 8195, 1, "int", 60, 80,
                 limit_regs=((8195, 3), (8195, 4)), unit="°C"),
        Writable("boiler_on", "Boiler on", 8197, 1, "bool", 0, 1, power=True),
    ]
}


@dataclass
class PendingWrite:
    key: str
    value: int
    source: str
    requested: float
    old: int | None
    sent: int = 0
    first_sent: float | None = None
    last_sent: float | None = None
    detail: list = field(default_factory=list)

    def as_dict(self) -> dict:
        w = WRITABLE[self.key]
        return {"key": self.key, "register": f"{w.reg},{w.idx}", "value": self.value,
                "old": self.old, "source": self.source, "requested": self.requested,
                "sent": self.sent, "last_sent": self.last_sent}


class WriteError(Exception):
    """A write request that was rejected; the message is shown to the user."""


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------

class Controller:
    def __init__(self, mode: str = "proxy", log_dir: str = "logs", *,
                 writes_enabled: bool = False, allow_power_writes: bool = False,
                 write_timeout: float = 120, write_min_interval: float = 10,
                 clock_sync_interval: float = 6 * 3600, tz=None,
                 state_file: str | None = None, clock=time.time) -> None:
        if mode not in ("proxy", "hybrid", "local"):
            raise ValueError(f"unknown MODE {mode!r} (proxy, hybrid or local)")
        self.mode = mode
        self.log_dir = log_dir
        self.writes_enabled = writes_enabled
        self.allow_power_writes = allow_power_writes
        self.write_timeout = write_timeout
        self.write_min_interval = write_min_interval
        self.clock_sync_interval = clock_sync_interval
        self.tz = tz or timezone.utc
        self.state_file = state_file or os.path.join(log_dir, "state.json")
        self.clock = clock
        self._lock = threading.RLock()

        self.registers: dict = {}            # (reg, idx) -> (ts, raw)
        self.base_read_list: list = list(P.DEFAULT_BASE_READ_LIST)
        self.read_list_source = "default"
        self.pending: dict = {}              # key -> PendingWrite
        self.last_request: dict = {}         # key -> ts of the last accepted request
        self.recent: list = []               # last audit entries for the UI
        self.last_report: float | None = None
        self.last_clock_sync: float | None = None
        self._rr = 0                         # round robin over pending writes
        self._saved_at = 0.0
        self._load_state()

    # -- persistence --------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return
        for k, (ts, raw) in (st.get("registers") or {}).items():
            try:
                reg, idx = (int(x) for x in k.split(","))
                self.registers[(reg, idx)] = (float(ts), int(raw))
            except (ValueError, TypeError):
                continue
        rl = st.get("base_read_list")
        if rl:
            self.base_read_list = [tuple(x) for x in rl]
            self.read_list_source = "saved"
        self.last_report = st.get("last_report")

    def save_state(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._saved_at < 60:
            return
        self._saved_at = now
        with self._lock:
            st = {
                "saved": now,
                "last_report": self.last_report,
                "base_read_list": [list(x) for x in self.base_read_list],
                "registers": {f"{r},{i}": [ts, raw] for (r, i), (ts, raw) in self.registers.items()},
            }
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_file)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            print(f"state save failed: {exc}", flush=True)

    # -- audit --------------------------------------------------------------

    def _audit(self, event: str, key: str, value=None, **extra) -> None:
        w = WRITABLE.get(key)
        now = self.clock()
        rec = {"time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
               "event": event, "key": key, "mode": self.mode}
        if w:
            rec["register"] = f"{w.reg},{w.idx}"
        if value is not None:
            rec["value"] = value
        rec.update({k: v for k, v in extra.items() if v is not None})
        self.recent.append(rec)
        del self.recent[:-50]
        print(f"write {event}: {key}={value} {extra if extra else ''}".rstrip(), flush=True)
        path = os.path.join(self.log_dir, f"writes-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"audit log write failed: {exc}", flush=True)

    # -- reading state ------------------------------------------------------

    def raw(self, reg: int, idx: int = 1) -> int | None:
        v = self.registers.get((reg, idx))
        return v[1] if v else None

    def current(self, key: str) -> int | None:
        w = WRITABLE[key]
        return w.readback(self.raw(w.reg, w.idx))

    def limits(self, key: str) -> tuple[int, int]:
        """Local hard limits intersected with the controller's min/max."""
        w = WRITABLE[key]
        lo, hi = w.lo, w.hi
        if w.limit_regs:
            cmin = self.raw(*w.limit_regs[0])
            cmax = self.raw(*w.limit_regs[1])
            if cmin is not None and 0 <= low16(cmin) <= 1000:
                lo = max(lo, low16(cmin))
            if cmax is not None and 0 <= low16(cmax) <= 1000:
                hi = min(hi, low16(cmax))
        return lo, hi

    def alarm_slot(self) -> int | None:
        c = self.raw(*P.ALARM_COUNTER)
        n = flag_value(c) if c is not None else None
        if n is None:
            return None
        return (n - 1) % P.ALARM_SLOTS

    def alarm_active(self) -> bool:
        bs = self.raw(*BURNER_STATE)
        if bs is not None and flag_value(bs) == BURNER_STATE_ERROR:
            return True
        slot = self.alarm_slot()
        if slot is None:
            return False
        code = self.raw(P.ALARM_CODE_BASE + slot)
        end = self.raw(P.ALARM_END_BASE + slot)
        if code is None:
            return False
        state = (code >> 16) & 0xFF
        return state in (3, 7) and not end

    # -- traffic ------------------------------------------------------------

    def observe_report(self, regs: dict) -> None:
        """Store the controller's report and settle pending writes."""
        now = self.clock()
        with self._lock:
            if regs:
                self.last_report = now
            for k, raw in regs.items():
                self.registers[k] = (now, raw)
            for key, pw in list(self.pending.items()):
                cur = self.current(key)
                if cur == pw.value:
                    del self.pending[key]
                    if pw.sent:
                        self._audit("confirmed", key, pw.value, old=pw.old, source=pw.source,
                                    polls=pw.sent, seconds=round(now - pw.requested, 1))
                    else:
                        self._audit("noop", key, pw.value, source=pw.source,
                                    reason="reported before it was sent")
                elif now - pw.requested > self.write_timeout:
                    del self.pending[key]
                    self._audit("failed", key, pw.value, old=pw.old, source=pw.source,
                                polls=pw.sent, reported=cur,
                                reason=f"no read-back within {self.write_timeout:g} s")
        self.save_state()

    def observe_cloud_reply(self, reply: str) -> None:
        """Learn the read list and see the cloud's own writes (proxy/hybrid)."""
        rl = P.parse_read_list(reply)
        with self._lock:
            if rl:
                base = [x for x in rl if not P.is_alarm_slot_reg(x[0])]
                if len(base) >= 20 and base != self.base_read_list:
                    self.base_read_list = base
                    self.read_list_source = "cloud"
                    self.save_state(force=True)
                elif len(base) >= 20:
                    self.read_list_source = "cloud"
            for reg, idx, val in P.parse_writes(reply):
                for key, pw in list(self.pending.items()):
                    w = WRITABLE[key]
                    if (w.reg, w.idx) == (reg, idx):
                        del self.pending[key]
                        self._audit("superseded", key, pw.value, source=pw.source,
                                    reason=f"cloud wrote {val} to the same register")

    def read_list(self) -> list:
        """Local ODCZYT: base list plus the newest two alarm slots."""
        out = list(self.base_read_list)
        slot = self.alarm_slot()
        if slot is not None:
            out += P.alarm_slot_regs(slot)
            out += P.alarm_slot_regs(slot - 1)
        return out

    def next_writes(self, local_reply: bool) -> list[tuple[int, int, int]]:
        """Entries for the ZAPIS of this poll (empty = none), marked as sent.

        ``local_reply`` is True when we build the whole reply (local mode or
        fallback); only then the clock sync is ours to send.
        """
        now = self.clock()
        with self._lock:
            if local_reply and self.mode == "local" and self.clock_sync_interval > 0 and (
                    self.last_clock_sync is None
                    or now - self.last_clock_sync >= self.clock_sync_interval):
                self.last_clock_sync = now
                t = P.local_unix_time(datetime.fromtimestamp(now, timezone.utc), self.tz)
                print(f"clock sync: {P.CLOCK_REG[0]},{P.CLOCK_REG[1]}={t}", flush=True)
                return [(P.CLOCK_REG[0], P.CLOCK_REG[1], t)]
            if not self.pending or not self.writes_enabled:
                return []
            alarm = self.alarm_active()
            keys = [k for k in self.pending if not (alarm and not WRITABLE[k].power)]
            if not keys:
                return []
            self._rr = (self._rr + 1) % len(keys)
            pw = self.pending[keys[self._rr]]
            w = WRITABLE[pw.key]
            pw.sent += 1
            pw.last_sent = now
            if pw.first_sent is None:
                pw.first_sent = now
                self._audit("sent", pw.key, pw.value, source=pw.source)
            return [(w.reg, w.idx, pw.value)]

    # -- requests from the UI / API ----------------------------------------

    def request_write(self, key: str, value, source: str = "web") -> dict:
        """Validate and queue a write. Raises WriteError when rejected."""
        try:
            return self._request_write(key, value, source)
        except WriteError as exc:
            self._audit("rejected", key, value if isinstance(value, (int, float)) else None,
                        source=source, reason=str(exc))
            raise

    def _request_write(self, key: str, value, source: str) -> dict:
        w = WRITABLE.get(key)
        if w is None:
            raise WriteError(f"{key!r} is not writable")
        if self.mode == "proxy":
            raise WriteError("writes need MODE=hybrid or MODE=local")
        if not self.writes_enabled:
            raise WriteError("writes are off (WRITES_ENABLED=0)")
        if w.power and not self.allow_power_writes:
            raise WriteError("switching the boiler on/off is off (ALLOW_POWER_WRITES=0)")
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise WriteError("value must be a whole number")
        lo, hi = self.limits(key)
        if not lo <= value <= hi:
            raise WriteError(f"{w.label} must be between {lo} and {hi}")
        if self.last_report is None:
            raise WriteError("no report from the controller yet")
        if not w.power and self.alarm_active():
            raise WriteError("an alarm is active; only on/off is allowed")
        now = self.clock()
        with self._lock:
            pend = self.pending.get(key)
            cur = self.current(key)
            if pend is None and cur == value:
                self._audit("noop", key, value, source=source, reason="already set")
                return {"status": "unchanged", "message": f"{w.label} is already {value}"}
            last = self.last_request.get(key)
            if last is not None and now - last < self.write_min_interval:
                raise WriteError(f"too fast, wait {self.write_min_interval - (now - last):.0f} s")
            self.last_request[key] = now
            if pend is not None and pend.value == value:
                return {"status": "pending", "message": f"{w.label} = {value} already queued"}
            if pend is not None:
                self._audit("replaced", key, pend.value, source=pend.source, new_value=value)
            if cur == value:  # a pending write back to the reported value: just drop it
                del self.pending[key]
                return {"status": "unchanged", "message": f"{w.label} is already {value}"}
            self.pending[key] = PendingWrite(key, value, source, now, cur)
            self._audit("requested", key, value, old=cur, source=source)
        return {"status": "pending",
                "message": f"{w.label} = {value} queued, sent on the next poll"}

    def cancel_write(self, key: str, source: str = "web") -> bool:
        with self._lock:
            pw = self.pending.pop(key, None)
        if pw:
            self._audit("cancelled", key, pw.value, source=source)
        return pw is not None

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            alarm = self.alarm_active() if self.last_report else False
            writable = {}
            for key, w in WRITABLE.items():
                lo, hi = self.limits(key)
                reason = None
                if self.mode == "proxy":
                    reason = "MODE=proxy (set MODE=hybrid or local)"
                elif not self.writes_enabled:
                    reason = "WRITES_ENABLED=0"
                elif w.power and not self.allow_power_writes:
                    reason = "ALLOW_POWER_WRITES=0"
                elif not w.power and alarm:
                    reason = "alarm active"
                writable[key] = {"label": w.label, "kind": w.kind, "unit": w.unit,
                                 "register": f"{w.reg},{w.idx}", "value": self.current(key),
                                 "min": lo, "max": hi, "allowed": reason is None,
                                 "reason": reason}
            return {
                "now": now,
                "mode": self.mode,
                "writes_enabled": self.writes_enabled,
                "allow_power_writes": self.allow_power_writes,
                "write_timeout": self.write_timeout,
                "alarm_active": alarm,
                "last_report": self.last_report,
                "read_list": {"source": self.read_list_source, "count": len(self.read_list())},
                "last_clock_sync": self.last_clock_sync,
                "writable": writable,
                "pending": [pw.as_dict() for pw in self.pending.values()],
                "recent": list(reversed(self.recent[-20:])),
            }
