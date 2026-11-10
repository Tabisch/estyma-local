"""Decoding of raw register values, and the one rule for corrupted reads.

The controller reads its values over CAN and now and then reports a
corrupted one, typically with garbage in the upper bits (705 -> 31981648) or
an 8-bit value without its 0x100 flag. Every part of the server decides
"is this value usable?" here, with the register definitions in
``catalog.py``:

- ``value(reg, raw)``: decoded value, or None for a corrupted read,
- ``LastGood``: keeps the last usable value of a key for a while, so one
  corrupted report does not make a value disappear,
- alarm slot helpers used by the monitor, the controller and MQTT.

The monitor page, the heating settings, the raw register page and the MQTT
publisher all use these, so they agree on which reads are glitches.
"""

from __future__ import annotations

import catalog as C

# Anything older than this (2017-07) in a time register is a glitch.
MIN_TIME = 1_500_000_000


# --------------------------------------------------------------------------
# Raw bit handling
# --------------------------------------------------------------------------

def low16(raw: int) -> int:
    """Corrupted CAN reads carry garbage in the upper 16 bits only."""
    return raw & 0xFFFF


def flag_value(raw: int) -> int | None:
    """0x100 | value style registers (256/257) -> value, else None."""
    v = low16(raw)
    return v & 0xFF if v >> 8 == 1 else None


def low8(raw: int) -> int | None:
    """8-bit values report as 0x0nn or 0x1nn; anything else is a glitch."""
    if raw is None or raw < 0 or raw >> 8 not in (0, 1):
        return None
    return raw & 0xFF


def signed16(raw: int) -> int:
    v = low16(raw)
    return v - 0x10000 if v >= 0x8000 else v


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def number(raw: int | None, how: str):
    """Raw value -> number by catalog decode name; None if the bits cannot be
    a valid value of that kind. "hex" and "alarm" return the raw value."""
    if raw is None:
        return None
    if how == "u8":
        return low8(raw)
    if how == "u8/10":
        v = low8(raw)
        return None if v is None else v / 10
    if how == "u16":
        return low16(raw)
    if how == "u16/10":
        return low16(raw) / 10
    if how == "s16":
        return signed16(raw)
    if how == "d10":
        return raw / 10
    if how == "d100":
        return raw / 100
    if how == "time":
        return raw if raw == 0 or raw >= MIN_TIME else None
    return raw  # "x1", "hex", "alarm"


def valid_range(reg: int) -> tuple | None:
    """Decoded values outside this range are corrupted reads."""
    r = C.BY_REG.get(reg)
    if r is None:
        return None
    return r.plausible or r.range


def value(reg: int, raw: int | None, idx: int = 1):
    """Raw report value of ``reg,idx`` -> decoded value, or None if missing or
    corrupted. Unknown registers are returned as is."""
    r = C.BY_REG.get(reg)
    if r is None:
        return raw
    v = number(raw, r.decode)
    if v is None or r.decode in ("hex", "alarm", "time"):
        return v
    rng = valid_range(reg)
    if rng is not None and not rng[0] <= v <= rng[1]:
        return None
    return v


def is_glitch(reg: int, raw: int | None, idx: int = 1) -> bool:
    """True if ``raw`` is present but not a usable value."""
    return raw is not None and value(reg, raw, idx) is None


class LastGood:
    """Remembers the last usable value per key, so a single corrupted report
    does not make a value disappear: ``get`` returns the new value if it is
    usable, else the last usable one for up to ``hold`` seconds."""

    def __init__(self, hold: float = 600) -> None:
        self.hold = hold
        self._good: dict = {}   # key -> (ts, value)

    def get(self, key, v, now: float):
        if v is not None:
            self._good[key] = (now, v)
            return v
        last = self._good.get(key)
        if last is not None and now - last[0] <= self.hold:
            return last[1]
        return None


# --------------------------------------------------------------------------
# Alarms
# --------------------------------------------------------------------------

def alarm_counter(raw: int | None) -> int | None:
    """16460,1 (0x100 + n) -> n."""
    return low8(raw)


def alarm_slot(counter_raw: int | None) -> int | None:
    """Newest alarm slot, (n - 1) % 20, from the raw counter."""
    n = alarm_counter(counter_raw)
    return None if n is None else (n - 1) % C.ALARM_SLOTS


def alarm_code(raw: int | None) -> tuple[int, str] | None:
    """16400+k -> (code, state name), or None if corrupted."""
    if raw is None or raw < 0:
        return None
    state = (raw >> 16) & 0xFFFF
    if raw >> 32 or state not in C.ALARM_STATES:
        return None
    return raw & 0xFF, C.ALARM_STATES[state]


def alarm_time(raw: int | None, now: float | None = None) -> int | None:
    """Start / acknowledge time (controller clock); 0 = not acknowledged."""
    if raw is None:
        return None
    if raw == 0:
        return 0
    if raw < MIN_TIME or (now is not None and raw > now + 2 * 86400):
        return None
    return raw


def alarm_is_active(code_raw: int | None, end_raw: int | None) -> bool:
    entry = alarm_code(code_raw)
    return entry is not None and entry[1] in ("active", "resetting") and not end_raw
