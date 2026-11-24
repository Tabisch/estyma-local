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
   writes are not verified for other settings. The exception is the weekly
   timer: like the cloud, all 7 days go in one ``ZAPIS`` (``request_timer``).
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

from . import protocol as P
from . import catalog as Cat
from . import decode as D
from . import registers as R

# --------------------------------------------------------------------------
# Register model
# --------------------------------------------------------------------------

BURNER_STATE = (8199, 1)
BURNER_STATE_ERROR = 10


# Weekly timer: one 24-bit mask per day, bit i = comfort in hour i.
TIMER_ALL = 0xFFFFFF
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def timer_text(mask: int | None) -> str | None:
    """Comfort hours of a day mask as ranges, end hour exclusive:
    0x3FFFC0 -> "06-22", two ranges -> "06-09,16-22", none -> ""."""
    if mask is None:
        return None
    out, h = [], 0
    while h < 24:
        if mask >> h & 1:
            start = h
            while h < 24 and mask >> h & 1:
                h += 1
            out.append(f"{start:02d}-{h:02d}")
        else:
            h += 1
    return ",".join(out)


def parse_timer_text(text: str) -> int:
    """Inverse of ``timer_text``; accepts "6-22", "06:00-22:00", spaces,
    "" / "none" for no comfort hours. Raises ValueError."""
    text = (text or "").strip().lower()
    if text in ("", "none", "-"):
        return 0
    mask = 0
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("-")
        if len(bits) != 2:
            raise ValueError(f"{part!r}: use start-end hours, e.g. 06-22")
        hours = []
        for b in bits:
            b = b.strip()
            if b.endswith(":00"):
                b = b[:-3]
            if not b.isdigit():
                raise ValueError(f"{part!r}: hours must be whole numbers 0..24")
            hours.append(int(b))
        start, end = hours
        if not (0 <= start < end <= 24):
            raise ValueError(f"{part!r}: start must be before end, both within 0..24")
        for h in range(start, end):
            mask |= 1 << h
    return mask


# Bit helpers live in decode.py (the one place that decides what a
# corrupted read is); kept importable from here.
from .decode import flag_value, low8, low16, signed16  # noqa: E402,F401


@dataclass(frozen=True)
class Writable:
    key: str
    label: str
    reg: int
    idx: int
    kind: str                 # "int", "bool", "enum" or "mask" (weekly timer day)
    lo: int                   # local hard limits in raw units (always applied)
    hi: int
    power: bool = False       # needs ALLOW_POWER_WRITES
    limit_regs: tuple | None = None  # (min reg, max reg) reported by the controller
    unit: str = ""
    label_de: str = ""
    decode: str = "u16"       # how a report reads back: "u16", "u8", "s16"
    scale: int = 1            # shown value = raw / scale
    tested: bool = False      # write seen working on the real controller
    group: str = ""           # settings page section
    options: dict | None = None  # enum: raw -> (en, de)
    conf: str = "confirmed"   # how sure the register mapping is
    main: bool = False        # shown in the monitor page's control panel

    def readback(self, raw: int | None) -> int | None:
        if raw is None:
            return None
        if self.kind == "bool":
            return flag_value(raw)
        if self.kind == "mask":   # 24 hour bits; anything above is a corrupted read
            return raw if 0 <= raw <= TIMER_ALL else None
        if self.decode == "u8":
            return low8(raw)
        if self.decode == "s16":
            return signed16(raw)
        return low16(raw)

    def show(self, raw: int | None):
        """Raw (write units) -> shown value."""
        if raw is None:
            return None
        return raw / self.scale if self.scale != 1 else raw

    def num(self, raw: int) -> str:
        return f"{raw / self.scale:.1f}" if self.scale != 1 else str(raw)

    def fmt(self, raw: int | None) -> str:
        if raw is None:
            return "?"
        if self.kind == "mask":
            return timer_text(raw)
        if self.options and raw in self.options:
            return self.options[raw][0]
        return f"{self.num(raw)} {self.unit}".strip()


def _setting(key, reg, group, lo=None, hi=None, *, kind="int", limits=True, options=None,
             tested=False, **kw) -> Writable:
    """A settings page entry; labels, decoding, unit, scale, confidence and the
    allowed range come from catalog.py.

    ``lo``/``hi`` (shown units, e.g. 5.0..15.0 %) narrow the catalog range;
    enums default to their value names. Stored in raw units.
    ``limits``: the controller also reports min/max as index 3/4.
    """
    r = Cat.get(reg)
    if options is None:
        options = r.values
    if r.range:
        lo = r.range[0] if lo is None else lo
        hi = r.range[1] if hi is None else hi
    elif options:
        lo = min(options) if lo is None else lo
        hi = max(options) if hi is None else hi
    if lo is None or hi is None:
        raise ValueError(f"setting {key}: register {reg} has no range in catalog.py")
    decode = {"u8/10": "u8", "u16/10": "u16", "d10": "u16", "x1": "u16"}.get(r.decode, r.decode)
    s = r.scale
    return Writable(key, kw.pop("label", r.label), reg, 1, kind, round(lo * s), round(hi * s),
                    limit_regs=((reg, 3), (reg, 4)) if limits else None, unit=r.unit,
                    label_de=kw.pop("label_de", r.label_de), decode=decode, scale=s, tested=tested,
                    group=group, options=options, conf=r.conf, **kw)


# Settings page sections, in order: key -> (English, German).
GROUPS = {
    "user": ("User settings", "Benutzereinstellungen"),
    "boiler": ("Boiler", "Kessel"),
    "burner": ("Burner", "Brenner"),
    "circuit1": ("Heating circuit 1 (CO1)", "Heizkreis 1 (CO1)"),
    "circuit2": ("Heating circuit 2 (CO2)", "Heizkreis 2 (CO2)"),
}

# Ranges are the ones the cloud panels offer (docs/registers.md). Only 8195,
# 8197 (2026-11-08), 8292 (2026-11-16) and 8468, 8469, 8242, 8264
# (2026-11-17) were seen being written by the cloud and taking effect; every other
# write is untested and needs ALLOW_UNTESTED_WRITES=1 as well.
_B = "burner"
_SETTINGS = [
    Writable("boiler_on", "Boiler on", 8197, 1, "bool", 0, 1, power=True, label_de="Kessel an",
             tested=True, group="user", options=R.VALUES[8197], main=True),
    _setting("boiler_target", 8195, "user", tested=True, main=True, label="Boiler target"),
    _setting("boiler_mode", 8194, "user", kind="enum", limits=False),
    _setting("buffer_top_target", 8468, "user", tested=True),
    _setting("buffer_bottom_target", 8469, "user", tested=True),
    _setting("room_comfort_1", 8242, "user", tested=True),
    _setting("room_eco_1", 8264, "user", tested=True),
    _setting("circuit1_program", 8292, "user", kind="enum", limits=False, tested=True),
    # Boiler (service panel "Kocioł")
    _setting("hyst_on", 8712, "boiler"),
    _setting("hyst_off", 8849, "boiler"),
    _setting("pump_min_temp", 8711, "boiler"),
    _setting("pump_overrun", 14088, "boiler"),
    _setting("return_min_temp", 8556, "boiler"),
    # Burner (service panel "Palnik"). Air correction goes down to -10 %, but
    # how negative values are written is unknown, so only 0..40 is offered.
    _setting("air_correction", 8819, _B, 0, 40),
    _setting("power_min", 8816, _B),
    _setting("power_max", 8817, _B),
    *[_setting(f"blower_{p}", 8800 + i, _B) for i, p in enumerate((20, 40, 60, 80, 100), 1)],
    _setting("blower_ignition", 8841, _B),
    _setting("initial_dose", 8790, _B),
    _setting("blower_glowing", 14084, _B),
    _setting("blower_burnout", 12300, _B),
    _setting("glowing_time", 8848, _B),
    _setting("burnout_time", 8811, _B),
    _setting("fuel_glowing", 10650, _B),
    _setting("feeder_output", 8818, _B),
    _setting("calorific_value", 8850, _B),
    _setting("lambda_control", 13829, _B, kind="enum", limits=False),
    _setting("o2_30", 8526, _B),
    _setting("o2_100", 8530, _B),
    _setting("ignition_min_time", 14092, _B),
    _setting("burner_feeder_pct", 14091, _B),
    _setting("feeder_period", 14080, _B),
    _setting("pause_time", 14101, _B),
    _setting("pause_power", 14102, _B),
    _setting("pause_blower", 14103, _B),
    _setting("feed_max", 8791, _B),
]
for _c in (1, 2):
    _g, _o = f"circuit{_c}", _c - 1  # CO2 registers are CO1 + 1
    _SETTINGS += [
        _setting(f"co{_c}_mode", 8514 + _o, _g, kind="enum", limits=False),
        _setting(f"co{_c}_manual_target", 8518 + _o, _g),
        _setting(f"co{_c}_curve_m20", 8901 + _o, _g),
        _setting(f"co{_c}_curve_0", 8905 + _o, _g),
        _setting(f"co{_c}_curve_10", 8909 + _o, _g),
    ]

# Weekly timer of heating circuit 1, Monday..Sunday (8486 + 4 * day). The
# cloud writes all 7 days in one ZAPIS (seen 2026-11-16); they are in the
# normal report, so no extra reads are needed.
TIMER_GROUP = "timer1"
TIMER_KEYS = tuple(f"co1_timer_{d}" for d in DAY_KEYS)
_TIMERS = [Writable(k, Cat.get(r).label, r, 1, "mask", 0, TIMER_ALL, label_de=Cat.get(r).label_de,
                    tested=True, group=TIMER_GROUP)
           for k, r in zip(TIMER_KEYS, Cat.WEEKLY_TIMERS)]

WRITABLE = {w.key: w for w in _SETTINGS + _TIMERS}

# Everything the settings page shows, read on demand (index 1 and min/max).
SETTINGS_READS = [k for w in _SETTINGS if not w.main
                  for k in [(w.reg, w.idx), *(w.limit_regs or ())]]
_SETTINGS_VALUES = [(w.reg, w.idx) for w in _SETTINGS if not w.main]


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
    batch: str | None = None   # written together in one ZAPIS (weekly timer)

    def as_dict(self) -> dict:
        w = WRITABLE[self.key]
        return {"key": self.key, "register": f"{w.reg},{w.idx}", "value": w.show(self.value),
                "raw": self.value, "old": w.show(self.old), "source": self.source,
                "requested": self.requested, "sent": self.sent, "last_sent": self.last_sent}


class WriteError(Exception):
    """A write request that was rejected; the message is shown to the user."""


class TooFast(WriteError):
    """Rejected only by the rate limit; the same request works a bit later."""

    def __init__(self, message: str, wait: float) -> None:
        super().__init__(message)
        self.wait = wait


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------

class Controller:
    def __init__(self, mode: str = "proxy", log_dir: str = "logs", *,
                 writes_enabled: bool = False, allow_power_writes: bool = False,
                 allow_untested_writes: bool = False, settings_read_interval: float = 3600,
                 write_timeout: float = 120, write_min_interval: float = 10,
                 clock_sync_interval: float = 6 * 3600, tz=None,
                 state_file: str | None = None, clock=time.time) -> None:
        if mode not in ("proxy", "hybrid", "local"):
            raise ValueError(f"unknown MODE {mode!r} (proxy, hybrid or local)")
        self.mode = mode
        self.log_dir = log_dir
        self.writes_enabled = writes_enabled
        self.allow_power_writes = allow_power_writes
        self.allow_untested_writes = allow_untested_writes
        self.settings_read_interval = settings_read_interval
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
        # Settings reads (SETTINGS_READS added to ODCZYT): due on start, on
        # request and every settings_read_interval; retried up to 3 polls
        # when the controller's next report does not carry them.
        self.settings_due = True
        self.settings_sent: float | None = None
        self.settings_read: float | None = None   # last report that carried them
        self._settings_tries = 0
        self._settings_attempt = self.clock()
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
        """Local hard limits intersected with the controller's min/max (raw units)."""
        w = WRITABLE[key]
        lo, hi = w.lo, w.hi
        if w.limit_regs:
            cmin = w.readback(self.raw(*w.limit_regs[0]))
            cmax = w.readback(self.raw(*w.limit_regs[1]))
            nlo = max(lo, cmin) if cmin is not None and 0 <= cmin <= 1000 else lo
            nhi = min(hi, cmax) if cmax is not None and 0 <= cmax <= 1000 else hi
            if nlo <= nhi:  # implausible limits (e.g. min > max) are ignored
                lo, hi = nlo, nhi
        return lo, hi

    def alarm_slot(self) -> int | None:
        return D.alarm_slot(self.raw(*P.ALARM_COUNTER))

    def alarm_active(self) -> bool:
        bs = self.raw(*BURNER_STATE)
        if bs is not None and D.value(BURNER_STATE[0], bs) == BURNER_STATE_ERROR:
            return True
        slot = self.alarm_slot()
        if slot is None:
            return False
        return D.alarm_is_active(self.raw(P.ALARM_CODE_BASE + slot), self.raw(P.ALARM_END_BASE + slot))

    # -- traffic ------------------------------------------------------------

    def observe_report(self, regs: dict) -> None:
        """Store the controller's report and settle pending writes."""
        now = self.clock()
        with self._lock:
            if regs:
                self.last_report = now
            for k, raw in regs.items():
                self.registers[k] = (now, raw)
            if self.settings_sent is not None and regs:
                got = sum(1 for k in _SETTINGS_VALUES if k in regs)
                if got >= len(_SETTINGS_VALUES) // 2:
                    self.settings_read, self.settings_sent = now, None
                    self._settings_tries = 0
                elif self._settings_tries < 3:
                    self.settings_due = True   # reply lost or ignored: ask again
                else:
                    self.settings_sent, self._settings_tries = None, 0
            # Timer writes whose changed days are not confirmed yet: their
            # unchanged days stay queued, so all 7 keep going out together.
            open_batches = {p.batch for k, p in self.pending.items()
                            if p.batch and self.current(k) != p.value}
            for key, pw in list(self.pending.items()):
                cur = self.current(key)
                if cur == pw.value and pw.batch and (not pw.sent or pw.batch in open_batches):
                    continue   # unchanged day of a timer write: still sent with the others
                if cur == pw.value:
                    del self.pending[key]
                    if pw.batch and pw.old == pw.value:
                        pass   # unchanged timer day, sent along with the others
                    elif pw.sent:
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
                    if (w.reg, w.idx) == (reg, idx) and key in self.pending:
                        drop = [key] + [k for k, p in self.pending.items()
                                        if pw.batch and p.batch == pw.batch and k != key]
                        for k in drop:
                            p = self.pending.pop(k)
                            self._audit("superseded", k, p.value, source=p.source,
                                        reason=f"cloud wrote {val} to the same register"
                                        if k == key else "cloud wrote the weekly timer")

    def read_list(self) -> list:
        """Local ODCZYT: base list plus the newest two alarm slots."""
        out = list(self.base_read_list)
        slot = self.alarm_slot()
        if slot is not None:
            out += P.alarm_slot_regs(slot)
            out += P.alarm_slot_regs(slot - 1)
        return out

    def request_settings_read(self) -> dict:
        if self.mode == "proxy":
            return {"ok": False, "message": "reading settings needs MODE=hybrid or MODE=local"}
        with self._lock:
            self.settings_due, self._settings_tries = True, 0
        return {"ok": True, "message": "settings are read on the next poll"}

    def extra_reads(self, present=()) -> list:
        """Registers to add to this poll's ODCZYT (hybrid/local only).

        The settings when due, plus every register with a pending write, so
        its read-back can confirm it. ``present``: entries already listed.
        """
        if self.mode == "proxy":
            return []
        now = self.clock()
        with self._lock:
            out = []
            if (not self.settings_due and self.settings_read_interval > 0
                    and self.settings_sent is None
                    and now - self._settings_attempt >= self.settings_read_interval):
                self.settings_due = True
            if self.settings_due:
                self.settings_due = False
                self.settings_sent = self._settings_attempt = now
                self._settings_tries += 1
                out += SETTINGS_READS
            for key in self.pending:
                w = WRITABLE[key]
                out += [(w.reg, w.idx), *(w.limit_regs or ())]
        seen = set(present)
        res = []
        for k in out:
            if k not in seen:
                seen.add(k)
                res.append(k)
        return res

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
            first = self.pending[keys[self._rr]]
            group = ([p for p in self.pending.values() if p.batch == first.batch]
                     if first.batch else [first])
            # Same order as the cloud's timer write: highest register first.
            group.sort(key=lambda p: WRITABLE[p.key].reg, reverse=True)
            out = []
            for pw in group:
                w = WRITABLE[pw.key]
                pw.sent += 1
                pw.last_sent = now
                if pw.first_sent is None:
                    pw.first_sent = now
                    if not (pw.batch and pw.old == pw.value):
                        self._audit("sent", pw.key, pw.value, source=pw.source)
                out.append((w.reg, w.idx, pw.value))
            return out

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
        if w.kind == "mask":   # a timer day is always written with the other 6
            return self._request_timer({key: value}, source)
        if self.mode == "proxy":
            raise WriteError("writes need MODE=hybrid or MODE=local")
        if not self.writes_enabled:
            raise WriteError("writes are off (WRITES_ENABLED=0)")
        if w.power and not self.allow_power_writes:
            raise WriteError("switching the boiler on/off is off (ALLOW_POWER_WRITES=0)")
        if not w.tested and not self.allow_untested_writes:
            raise WriteError("writing this register is untested (ALLOW_UNTESTED_WRITES=0)")
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)) or value != value:
            raise WriteError("value must be a number")
        raw = value * w.scale
        if isinstance(raw, float):
            if abs(raw - round(raw)) > 1e-6:
                raise WriteError("value must be a whole number" if w.scale == 1
                                 else f"value must be a multiple of {1 / w.scale:g}")
            raw = int(round(raw))
        value = raw
        lo, hi = self.limits(key)
        if not lo <= value <= hi:
            raise WriteError(f"{w.label} must be between {w.num(lo)} and {w.num(hi)}"
                             if not w.options else f"{w.label}: value not allowed")
        if w.options and value not in w.options:
            raise WriteError(f"{w.label}: value not allowed")
        if self.last_report is None:
            raise WriteError("no report from the controller yet")
        if self.raw(w.reg, w.idx) is None:
            raise WriteError(f"{w.label} has not been read yet, read the settings first")
        if not w.power and self.alarm_active():
            raise WriteError("an alarm is active; only on/off is allowed")
        now = self.clock()
        with self._lock:
            pend = self.pending.get(key)
            cur = self.current(key)
            if pend is None and cur == value:
                self._audit("noop", key, value, source=source, reason="already set")
                return {"status": "unchanged", "message": f"{w.label} is already {w.fmt(value)}"}
            last = self.last_request.get(key)
            if last is not None and now - last < self.write_min_interval:
                wait = self.write_min_interval - (now - last)
                raise TooFast(f"too fast, wait {wait:.0f} s", wait)
            self.last_request[key] = now
            if pend is not None and pend.value == value:
                return {"status": "pending", "message": f"{w.label} = {w.fmt(value)} already queued"}
            if pend is not None:
                self._audit("replaced", key, pend.value, source=pend.source, new_value=value)
            if cur == value:  # a pending write back to the reported value: just drop it
                del self.pending[key]
                return {"status": "unchanged", "message": f"{w.label} is already {w.fmt(value)}"}
            self.pending[key] = PendingWrite(key, value, source, now, cur)
            self._audit("requested", key, value, old=cur, source=source)
        return {"status": "pending",
                "message": f"{w.label} = {w.fmt(value)} queued, sent on the next poll"}

    def cancel_write(self, key: str, source: str = "web") -> bool:
        with self._lock:
            pw = self.pending.pop(key, None)
            batch = [] if pw is None or not pw.batch else [
                self.pending.pop(k) for k, p in list(self.pending.items()) if p.batch == pw.batch]
        for p in [pw, *batch] if pw else []:
            self._audit("cancelled", p.key, p.value, source=source)
        return pw is not None

    def timer_masks(self) -> list:
        """Day masks Monday..Sunday as the controller reports them (None = not read)."""
        return [self.current(k) for k in TIMER_KEYS]

    def request_timer(self, days, source: str = "web") -> dict:
        """Queue a weekly timer write: ``days`` = 7 masks Monday..Sunday, or a
        dict {day key or "mon".."sun": mask} that changes only those days.

        All 7 days are written in one ZAPIS, like the cloud does, and each
        day is confirmed by its read-back. Raises WriteError when rejected.
        """
        try:
            return self._request_timer(days, source)
        except WriteError as exc:
            self._audit("rejected", TIMER_KEYS[0], None, source=source, reason=str(exc))
            raise

    def _request_timer(self, days, source: str) -> dict:
        w0 = WRITABLE[TIMER_KEYS[0]]
        if self.mode == "proxy":
            raise WriteError("writes need MODE=hybrid or MODE=local")
        if not self.writes_enabled:
            raise WriteError("writes are off (WRITES_ENABLED=0)")
        if self.last_report is None:
            raise WriteError("no report from the controller yet")
        with self._lock:
            # Start from what is pending, else reported, so two quick edits of
            # different days do not undo each other.
            base = [self.pending[k].value if k in self.pending else self.current(k)
                    for k in TIMER_KEYS]
        if isinstance(days, dict):
            new = list(base)
            for k, v in days.items():
                k = str(k)
                k = k[len("co1_timer_"):] if k.startswith("co1_timer_") else k
                if k not in DAY_KEYS:
                    raise WriteError(f"unknown day {k!r}")
                new[DAY_KEYS.index(k)] = v
        elif isinstance(days, (list, tuple)) and len(days) == 7:
            new = list(days)
        else:
            raise WriteError("days must be 7 masks, Monday..Sunday")
        for i, v in enumerate(new):
            if isinstance(v, str):
                try:
                    v = parse_timer_text(v)
                except ValueError as exc:
                    raise WriteError(str(exc)) from None
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= TIMER_ALL:
                raise WriteError(f"{WRITABLE[TIMER_KEYS[i]].label}: value must be a 24-bit hour mask")
            new[i] = v
        if any(m is None for m in self.timer_masks()):
            raise WriteError(f"{w0.label} has not been read yet, wait for the next report")
        if self.alarm_active():
            raise WriteError("an alarm is active; only on/off is allowed")
        now = self.clock()
        with self._lock:
            cur = self.timer_masks()
            pending = {k: self.pending[k].value for k in TIMER_KEYS if k in self.pending}
            if new == cur and not pending:
                self._audit("noop", TIMER_KEYS[0], None, source=source, reason="already set")
                return {"status": "unchanged", "message": "weekly timer is already set"}
            if pending and all(pending.get(k, c) == v for k, c, v in zip(TIMER_KEYS, cur, new)):
                return {"status": "pending", "message": "weekly timer already queued"}
            last = max((self.last_request.get(k) or 0) for k in TIMER_KEYS)
            if last and now - last < self.write_min_interval:
                wait = self.write_min_interval - (now - last)
                raise TooFast(f"too fast, wait {wait:.0f} s", wait)
            for k in TIMER_KEYS:
                self.last_request[k] = now
                old = self.pending.pop(k, None)
                if old is not None:
                    self._audit("replaced", k, old.value, source=old.source)
            if new == cur:   # back to what the controller has: nothing to send
                return {"status": "unchanged", "message": "weekly timer is already set"}
            batch = f"timer-{now:.3f}"
            for k, c, v in zip(TIMER_KEYS, cur, new):
                self.pending[k] = PendingWrite(k, v, source, now, c, batch=batch)
                if c != v:
                    self._audit("requested", k, v, old=c, source=source)
        changed = sum(1 for c, v in zip(cur, new) if c != v)
        return {"status": "pending",
                "message": f"weekly timer ({changed} day{'s' if changed != 1 else ''} changed) "
                           "queued, sent on the next poll"}

    def timers_snapshot(self, alarm: bool) -> dict:
        """The weekly timer for the heating settings page."""
        w0 = WRITABLE[TIMER_KEYS[0]]
        reason = self._lock_reason(w0, alarm)
        days = []
        for d, k in zip(DAY_KEYS, TIMER_KEYS):
            w = WRITABLE[k]
            reg = self.registers.get((w.reg, w.idx))
            pw = self.pending.get(k)
            days.append({"key": k, "day": d, "register": f"{w.reg},{w.idx}",
                         "label": w.label, "label_de": w.label_de, "value": self.current(k),
                         "seen": reg[0] if reg else None,
                         "pending": pw.value if pw else None, "sent": pw.sent if pw else 0})
        return {"circuit": 1, "allowed": reason is None, "reason": reason, "days": days}

    def _lock_reason(self, w: Writable, alarm: bool) -> str | None:
        if self.mode == "proxy":
            return "MODE=proxy (set MODE=hybrid or local)"
        if not self.writes_enabled:
            return "WRITES_ENABLED=0"
        if w.power and not self.allow_power_writes:
            return "ALLOW_POWER_WRITES=0"
        if not w.tested and not self.allow_untested_writes:
            return "ALLOW_UNTESTED_WRITES=0"
        if not w.power and alarm:
            return "alarm active"
        return None

    def _entry(self, key: str, w: Writable, alarm: bool) -> dict:
        lo, hi = self.limits(key)
        reason = self._lock_reason(w, alarm)
        reg = self.registers.get((w.reg, w.idx))
        cur = self.current(key)
        return {"label": w.label, "label_de": w.label_de, "kind": w.kind, "unit": w.unit,
                "register": f"{w.reg},{w.idx}", "value": w.show(cur), "raw": reg[1] if reg else None,
                "seen": reg[0] if reg else None, "min": w.show(lo), "max": w.show(hi),
                "step": 1 / w.scale, "options": ({str(k): list(v) for k, v in w.options.items()
                                                  if lo <= k <= hi} if w.options else None),
                "tested": w.tested, "conf": w.conf, "allowed": reason is None, "reason": reason}

    def snapshot(self) -> dict:
        """State for the monitor page's control panel (main settings only)."""
        now = self.clock()
        with self._lock:
            alarm = self.alarm_active() if self.last_report else False
            writable = {k: self._entry(k, w, alarm) for k, w in WRITABLE.items() if w.main}
            return {
                "now": now,
                "mode": self.mode,
                "writes_enabled": self.writes_enabled,
                "allow_power_writes": self.allow_power_writes,
                "allow_untested_writes": self.allow_untested_writes,
                "write_timeout": self.write_timeout,
                "alarm_active": alarm,
                "last_report": self.last_report,
                "read_list": {"source": self.read_list_source, "count": len(self.read_list())},
                "last_clock_sync": self.last_clock_sync,
                "writable": writable,
                "pending": [pw.as_dict() for pw in self.pending.values()],
                "recent": list(reversed(self.recent[-20:])),
            }

    def settings_snapshot(self) -> dict:
        """All settings, grouped, for the settings page."""
        now = self.clock()
        with self._lock:
            alarm = self.alarm_active() if self.last_report else False
            groups = []
            for g, (en, de) in GROUPS.items():
                items = [dict(self._entry(k, w, alarm), key=k)
                         for k, w in WRITABLE.items() if w.group == g]
                groups.append({"key": g, "label": en, "label_de": de, "items": items})
            return {
                "now": now,
                "mode": self.mode,
                "writes_enabled": self.writes_enabled,
                "allow_power_writes": self.allow_power_writes,
                "allow_untested_writes": self.allow_untested_writes,
                "write_timeout": self.write_timeout,
                "alarm_active": alarm,
                "last_report": self.last_report,
                "settings": {"due": self.settings_due, "sent": self.settings_sent,
                             "read": self.settings_read,
                             "interval": self.settings_read_interval,
                             "can_read": self.mode != "proxy"},
                "groups": groups,
                "timers": self.timers_snapshot(alarm),
                "pending": [pw.as_dict() for pw in self.pending.values()],
                "recent": list(reversed(self.recent[-20:])),
            }
