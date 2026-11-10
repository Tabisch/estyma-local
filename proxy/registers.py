"""Raw register table: every register seen in the traffic, with its latest value.

Fed by ``Monitor`` with each controller report (``ZAPISZ_ODCZYT``) and the
read list the server answered with (``ODCZYT``). The table only ever grows: a
register that shows up for the first time (for example one the cloud's
service panel added to the read list) gets a row of its own, labelled from
``CATALOG`` or "unknown". The table is saved to ``registers.json`` in the log
directory, so registers that were read only once survive a restart.

Corrupted CAN reads last for one report, so a value only counts as "stable"
once two reports in a row agree. The page shows the stable value. A newer raw
value that differs from it is normally just a change waiting for its second
report; it is only flagged as a glitch when it looks corrupted: when the
monitor and MQTT would drop it (``decode.py``), or see ``looks_glitched``.

The web UI (log_ui.py) serves the page (registers.html) and ``/api/registers``.
"""

from __future__ import annotations

import json
import os
import threading
import time

# What is known about each register (labels, decoding, ranges, value names)
# lives in catalog.py; these names stay importable from here.
from catalog import CATALOG, RANGES, UNKNOWN, VALUES  # noqa: F401
import decode as D


def decode(raw: int, how: str):
    """Raw report value -> number or text for the page (None if not decodable)."""
    if raw is None:
        return None
    scale = 1
    if how.endswith("/10"):
        how, scale = how[:-3], 10
    if how == "u8":
        val = raw & 0xFF
    elif how == "u16":
        val = raw & 0xFFFF
    elif how == "s16":
        val = raw & 0xFFFF
        val = val - 0x10000 if val >= 0x8000 else val
    elif how == "d10":
        val, scale = raw, 10
    elif how == "d100":
        val, scale = raw, 100
    elif how == "hex":
        return f"0x{raw & 0xFFFFFFFF:X}"
    elif how == "alarm":
        return f"code {raw & 0xFF}, state {(raw >> 16) & 0xFFFF}"
    elif how == "time":
        return raw  # the page formats it (controller clock = local time as UTC)
    else:
        val = raw
    return val / scale if scale != 1 else val


# Decodes whose raw value is a plain number, so a huge jump means corruption.
# "time", "hex" and "alarm" registers legitimately change in their upper bits.
_NUMERIC = ("x1", "d10", "d100", "u8", "u16", "s16", "u8/10", "u16/10")
# Corrupted reads seen so far keep the low 16 bits and garble the upper ones
# (e.g. 705 -> 31981648), so a jump this big between two reports is a glitch.
GLITCH_JUMP = 0xFFFF


def looks_glitched(raw, stable, how: str, rng=None) -> bool:
    """True if ``raw`` (not yet confirmed) looks like a corrupted read of a
    register whose last stable value is ``stable``. A normal change, such as
    a temperature moving by 0.1 degrees, is not a glitch: it is simply waiting
    for the second report that confirms it."""
    if raw is None or stable is None or raw == stable or how not in _NUMERIC:
        return False
    if abs(raw - stable) > GLITCH_JUMP:
        return True
    if rng is not None:
        val = decode(raw, how)
        if isinstance(val, (int, float)) and not rng[0] <= val <= rng[1]:
            return True
    return False


# --------------------------------------------------------------------------
# Table
# --------------------------------------------------------------------------

SAVE_INTERVAL_S = 60


class RegisterTable:
    def __init__(self, log_dir: str | None = None, filename: str = "registers.json") -> None:
        self.path = os.path.join(log_dir, filename) if log_dir else None
        self._lock = threading.Lock()
        # (reg, idx) -> {first, last, raw, stable, stable_t, count, changes, requested}
        self.entries: dict = {}
        self._dirty = False
        self._new = False   # a register was added since the last save
        self._saved = 0.0
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            for e in data.get("registers", []):
                key = (int(e.pop("reg")), int(e.pop("idx")))
                self.entries[key] = e
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def save(self, force: bool = False) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty or (not force and time.time() - self._saved < SAVE_INTERVAL_S
                                   and not self._new):
                return
            rows = [dict(e, reg=r, idx=i) for (r, i), e in sorted(self.entries.items())]
            self._dirty = self._new = False
            self._saved = time.time()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "registers": rows}, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- feeding ------------------------------------------------------------

    def observe(self, ts: float, regs: dict, requested=None) -> None:
        """One report {(reg, idx): raw} at ``ts``; ``requested``: the read list."""
        with self._lock:
            for key, raw in regs.items():
                e = self.entries.get(key)
                if e is None:
                    e = self.entries[key] = {"first": ts, "last": None, "raw": None, "stable": None,
                                             "stable_t": None, "count": 0, "changes": 0}
                    self._new = True
                if e["first"] is None or ts < e["first"]:
                    e["first"] = ts
                if e["last"] is not None and ts <= e["last"]:
                    continue  # older record (log replay after a restart)
                e["count"] += 1
                if raw == e["raw"] and e["stable"] != raw:
                    if e["stable"] is not None:
                        e["changes"] += 1
                    e["stable"] = raw
                if raw == e["stable"]:
                    e["stable_t"] = ts
                e["raw"], e["last"] = raw, ts
                self._dirty = True
            for key in requested or ():
                e = self.entries.get(key)
                if e is None:
                    e = self.entries[key] = {"first": None, "last": None, "raw": None, "stable": None,
                                             "stable_t": None, "count": 0, "changes": 0}
                    self._new = True
                if (e.get("requested") or 0) < ts:
                    e["requested"] = ts
                    self._dirty = True
        self.save()

    # -- reading ------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            items = sorted(self.entries.items())
            items = [(k, dict(e)) for k, e in items]
        out = []
        for (reg, idx), e in items:
            label, label_de, how, unit, conf = CATALOG.get(reg, UNKNOWN)
            if idx not in (1, 3, 4) and conf != "unknown":
                conf = "guess"  # only index 1/3/4 (value/min/max) were ever seen
            shown = e["stable"] if e["stable"] is not None else e["raw"]
            value = decode(shown, how)
            name = VALUES.get(reg, {}).get(value) if idx == 1 else None
            rng = RANGES.get(reg) if idx == 1 else None
            pending = e["stable"] is not None and e["raw"] is not None and e["raw"] != e["stable"]
            # Same rule as the monitor and MQTT (decode.py): a value they would
            # drop is a glitch here too, unless the stable value is just as odd.
            bad = (idx == 1 and D.is_glitch(reg, e["raw"]) and not D.is_glitch(reg, e["stable"]))
            out.append({
                "reg": reg, "idx": idx, "label": label, "label_de": label_de, "unit": unit,
                "conf": conf, "decode": how, "range": rng,
                "value_name": list(name) if name else None,
                "raw": e["raw"], "stable": e["stable"], "value": value,
                "pending": pending, "glitch": pending and (bad or looks_glitched(e["raw"], e["stable"], how, rng)),
                "first": e["first"], "last": e["last"], "stable_t": e.get("stable_t"),
                "count": e["count"], "changes": e["changes"], "requested": e.get("requested"),
            })
        return out
