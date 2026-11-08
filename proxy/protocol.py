"""Parsing and building of the controller <-> server messages.

The controller sends one ``POST /device/connect`` per poll. Its body is a
loose ``k=v&`` form (not URL-encoded, NUL-padded, names with spaces). The
server answers with a single text line holding, in this order:

    Next Daystat Update: <date> (GMT 1) --
    ZAPIS&;reg,idx=value;...;4112,1=1;&KONIEC<br>      (optional, writes)
    ODCZYT&<count>;reg,idx;...;KONIEC&<br>             (registers to report)
    SERVER_INFO=REGISTER,1;DELAY,10;<br><br>wygenerowano w: 0.0400 sec.

Everything here is pure string handling, no I/O, so it can be unit tested.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

# Commit/"apply" flag the cloud sends with every write.
APPLY_FLAG = (4112, 1)
# Controller clock (local time as a Unix timestamp), write only.
CLOCK_REG = (8549, 1)

# Alarm / event ring buffers: slot k uses code 16400+k, start 16420+k,
# end 16440+k (see docs/register-mapping.md). 16460,1 counts alarms.
ALARM_COUNTER = (16460, 1)
ALARM_SLOTS = 20
ALARM_CODE_BASE, ALARM_START_BASE, ALARM_END_BASE = 16400, 16420, 16440

# The registers the cloud asks for on every poll, without the rotating alarm
# slot entries. Used in local mode until a list was learned from the cloud.
DEFAULT_BASE_READ_LIST = [
    (8548, 1), (8485, 1), (8453, 1), (8454, 1), (8455, 1), (8456, 1), (8483, 1),
    (16460, 1), (16461, 1), (8484, 1), (16392, 1), (16384, 1), (16385, 1),
    (16386, 1), (16387, 1), (16388, 1), (16389, 1), (16391, 1), (16390, 1),
    (8193, 1), (8195, 1), (8195, 3), (8195, 4), (8196, 1), (8198, 1), (8473, 1),
    (8480, 1), (8194, 1), (8197, 1), (8199, 1), (13825, 1), (8543, 1), (13864, 1),
    (8823, 1), (8720, 1), (8865, 1), (8214, 1), (8242, 1), (8242, 3), (8242, 4),
    (8264, 1), (8264, 3), (8264, 4), (8292, 1), (8857, 1), (8466, 1), (8467, 1),
    (8468, 1), (8468, 3), (8468, 4), (8469, 1), (8469, 3), (8469, 4), (8471, 1),
    (8486, 1), (8490, 1), (8494, 1), (8498, 1), (8502, 1), (8506, 1), (8510, 1),
]


def is_alarm_slot_reg(reg: int) -> bool:
    return ALARM_CODE_BASE <= reg < ALARM_END_BASE + ALARM_SLOTS


def alarm_slot_regs(slot: int) -> list[tuple[int, int]]:
    """Read list entries of one alarm slot, in the cloud's order (end, start, code)."""
    slot %= ALARM_SLOTS
    return [(ALARM_END_BASE + slot, 1), (ALARM_START_BASE + slot, 1), (ALARM_CODE_BASE + slot, 1)]


# --------------------------------------------------------------------------
# Request (controller -> server)
# --------------------------------------------------------------------------

def loose_form(text: str) -> dict:
    """Split the NUL-padded, not URL-encoded form body of the controller."""
    out = {}
    for part in text.rstrip("\0").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
    return out


def parse_report(value: str) -> dict:
    """``ZAPISZ_ODCZYT`` ("reg,idx,value;...") -> {(reg, idx): raw int}."""
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


# --------------------------------------------------------------------------
# Reply (server -> controller)
# --------------------------------------------------------------------------

_ZAPIS_RE = re.compile(r"ZAPIS&;(.*?)&KONIEC")
_ODCZYT_RE = re.compile(r"ODCZYT&(\d*);(.*?)KONIEC&")


def parse_writes(reply: str) -> list[tuple[int, int, int]]:
    """``ZAPIS`` entries of a reply as (reg, idx, value), in order."""
    m = _ZAPIS_RE.search(reply)
    if not m:
        return []
    out = []
    for item in m.group(1).split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        bits = key.split(",")
        try:
            out.append((int(bits[0]), int(bits[1]) if len(bits) > 1 else 1, int(val)))
        except (ValueError, IndexError):
            pass
    return out


def parse_read_list(reply: str) -> list[tuple[int, int]] | None:
    """``ODCZYT`` list of a reply. Reads until ``KONIEC``, the count can be off."""
    m = _ODCZYT_RE.search(reply)
    if not m:
        return None
    out = []
    for item in m.group(2).split(";"):
        bits = item.strip().split(",")
        if len(bits) != 2:
            continue
        try:
            out.append((int(bits[0]), int(bits[1])))
        except ValueError:
            pass
    return out


def format_writes(entries) -> str:
    """``ZAPIS`` block for [(reg, idx, value), ...]; the apply flag is added."""
    body = "".join(f"{r},{i}={v};" for r, i, v in entries if (r, i) != APPLY_FLAG)
    return f"ZAPIS&;{body}{APPLY_FLAG[0]},{APPLY_FLAG[1]}=1;&KONIEC<br>"


def inject_writes(reply: str, entries) -> str | None:
    """Insert a ``ZAPIS`` block in front of the ``ODCZYT`` block of a reply.

    Returns None when the reply cannot take it safely: it already carries a
    ``ZAPIS`` (multi-register writes are not verified yet) or has no
    ``ODCZYT`` block (an error page, not a normal reply).
    """
    if not entries or not can_inject(reply):
        return None
    pos = reply.find("ODCZYT&")
    return reply[:pos] + format_writes(entries) + reply[pos:]


def add_reads(reply: str, entries) -> str | None:
    """Append ``reg,idx`` entries to the ``ODCZYT`` list of a reply.

    This is how the cloud's service panel reads settings: the controller
    reports every listed register on its next request. Entries already in
    the list are skipped. Returns None when nothing was added or the reply
    has no ``ODCZYT`` block.
    """
    m = _ODCZYT_RE.search(reply)
    if not m or not entries:
        return None
    have = set(parse_read_list(reply) or ())
    new = [e for e in dict.fromkeys(entries) if e not in have]
    if not new:
        return None
    items = m.group(2)
    if items and not items.endswith(";"):
        items += ";"
    count = len(have) + len(new)
    block = f"ODCZYT&{count};{items}" + "".join(f"{r},{i};" for r, i in new) + "KONIEC&"
    return reply[:m.start()] + block + reply[m.end():]


def can_inject(reply: str) -> bool:
    if "ZAPIS&" in reply:
        return False
    pos = reply.find("ODCZYT&")
    return pos >= 0 and "KONIEC&" in reply[pos:]


def build_reply(read_list, writes=(), now: datetime | None = None,
                tz: tzinfo | None = None, delay: int = 10) -> str:
    """A reply in the same layout as the cloud's."""
    tz = tz or timezone.utc
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    nxt = (now + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    off = now.utcoffset() or timedelta(0)
    hours = off.total_seconds() / 3600
    gmt = f"{hours:+g}".lstrip("+") if hours else "0"
    regs = "".join(f"{r},{i};" for r, i in read_list)
    return (
        f"Next Daystat Update: {nxt} (GMT {gmt}) -- "
        + (format_writes(writes) if writes else "")
        + f"ODCZYT&{len(read_list)};{regs}KONIEC&<br>"
        + f"SERVER_INFO=REGISTER,1;DELAY,{int(delay)};<br><br>wygenerowano w: 0.0010 sec."
    )


def local_unix_time(now: datetime, tz: tzinfo) -> int:
    """Unix timestamp shifted to local wall time, as the controller clock expects."""
    local = now.astimezone(tz)
    return int(local.timestamp() + (local.utcoffset() or timedelta(0)).total_seconds())
